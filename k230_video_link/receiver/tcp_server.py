"""
接收端 TCP Server 和视频帧接收线程。

职责边界：
    只拥有 listen socket、client socket 和接收线程。
    每收完一帧后通过回调交给上层，不导入 VDEC、Display 或 MP4。
"""

import _thread
import socket
import time

from common.frame_protocol import receive_frame
from receiver.config import (
    RECEIVER_IP,
    TCP_LISTEN_BACKLOG,
    TCP_PORT,
)


def close_socket(sock):
    """安全关闭 socket，并忽略清理阶段的次要异常。"""

    if sock is None:
        return

    try:
        sock.close()
    except BaseException:
        pass


class VideoFrameServer:
    """
    接受一个发送端连接，并把完整帧交给回调。

    回调：
        on_frame:
            每收到一帧调用，参数为 flags、frame_id、timestamp_ms、payload。
        on_connected:
            发送端刚建立连接时调用。
        on_disconnected:
            当前连接结束时调用。
        on_error:
            网络线程发生异常时调用。

    本类只拥有 socket，不导入 VDEC、Display 或 MP4。
    """

    def __init__(
        self,
        on_frame,
        on_connected=None,
        on_disconnected=None,
        on_error=None,
    ):
        self.on_frame = on_frame
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_error = on_error

        self.listen_socket = None
        self.client_socket = None
        self.running = False
        self.worker_stopped = True

    def _create_listen_socket(self):
        """
        创建、绑定并监听 TCP Server socket。

        SO_REUSEADDR 让程序停止后能够更快重新绑定同一端口。旧固件不支持时
        继续使用默认 socket 配置。
        """

        listen_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        try:
            listen_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )
        except BaseException:
            pass

        listen_socket.bind((RECEIVER_IP, TCP_PORT))
        listen_socket.listen(TCP_LISTEN_BACKLOG)
        return listen_socket

    def _notify(self, callback, *args):
        """
        安全调用可选回调。

        UI 或录像回调自身的异常不能直接杀死 TCP 接收线程。
        """

        if callback is None:
            return

        try:
            callback(*args)
        except BaseException as error:
            print("Receiver callback failed:", error)

    def _serve_client(self, client_socket):
        """
        持续接收并分发完整帧，直到当前客户端断开。

        receive_frame() 内部会先收满 24 字节帧头，再收满 payload_length
        指定的 H.264 数据。
        """

        while self.running:
            (
                flags,
                frame_id,
                timestamp_ms,
                payload,
            ) = receive_frame(client_socket)

            self.on_frame(
                flags,
                frame_id,
                timestamp_ms,
                payload,
            )

    def _worker(self):
        """
        后台 accept 循环。

        当前发送端断开后不会结束线程，而是关闭旧 client socket，继续等待
        同一个或新的发送端重新连接。
        """

        self.worker_stopped = False

        try:
            self.listen_socket = self._create_listen_socket()
            print(
                "TCP server listening on %s:%d"
                % (RECEIVER_IP, TCP_PORT)
            )

            while self.running:
                client_socket = None

                try:
                    client_socket, client_address = (
                        self.listen_socket.accept()
                    )
                    self.client_socket = client_socket

                    try:
                        client_socket.setsockopt(
                            socket.IPPROTO_TCP,
                            socket.TCP_NODELAY,
                            1,
                        )
                    except BaseException:
                        pass

                    print("Transmitter connected:", client_address)
                    self._notify(
                        self.on_connected,
                        client_address,
                    )
                    self._serve_client(client_socket)

                except KeyboardInterrupt:
                    raise

                except BaseException as error:
                    if self.running:
                        print("Client connection ended:", error)
                        self._notify(self.on_error, error)

                finally:
                    close_socket(client_socket)
                    self.client_socket = None
                    self._notify(self.on_disconnected)

                if self.running:
                    time.sleep(0.2)

        except KeyboardInterrupt:
            pass

        except BaseException as error:
            if self.running:
                print("TCP server failed:", error)
                self._notify(self.on_error, error)

        finally:
            close_socket(self.client_socket)
            close_socket(self.listen_socket)
            self.client_socket = None
            self.listen_socket = None
            self.worker_stopped = True
            print("TCP server stopped.")

    def start(self):
        """启动后台网络接收线程。"""

        if self.running:
            return

        self.running = True
        self.worker_stopped = False
        _thread.start_new_thread(self._worker, ())

    def stop(self):
        """
        关闭监听与客户端 socket，并等待后台线程退出。

        等待最多约 3 秒，避免固件 socket 未立即唤醒时永久卡住清理流程。
        """

        if not self.running:
            return

        self.running = False
        close_socket(self.client_socket)
        close_socket(self.listen_socket)

        wait_count = 0

        while not self.worker_stopped and wait_count < 30:
            time.sleep(0.1)
            wait_count += 1
