# -*- coding: utf-8 -*-
"""
K230 固定 IP 热点 RTSP 图传发送端。

完整数据链路：
    摄像头采集图像
        -> K230 硬件编码器压缩为 H.264
        -> RTSP 服务通过 Wi-Fi 热点发送
        -> Pad 使用播放器接收、录制和回放。

使用方法：
    1. K230 自己创建 Wi-Fi 热点 K230_RTSP，不依赖赛场路由器。
    2. Pad 连接该热点，并把自身静态 IP 设置为 192.168.8.2。
    3. Pad 播放下面的固定地址：
        rtsp://192.168.8.1:8554/test

自启方法：
    把本文件保存为 K230 SD 卡根目录下的 /sdcard/main.py。
    CanMV 固件启动 MicroPython 后会自动执行 main.py。
"""

# _thread：创建逐帧取流和发送数据的后台线程。
import _thread

# gc：服务重启后主动回收不再使用的对象，减少长期运行时的内存碎片。
import gc

# os：使用 CanMV 的 exitpoint，让 IDE 停止按钮能够打断循环。
import os

# time：冷启动等待、超时判断和重启延时。
import time

# uctypes：根据编码器返回的内存地址和长度读取底层 H.264 数据。
import uctypes

# image：创建一张用于绘制中文状态文字的 LCD 图像。
import image

# network：创建 K230 的 Wi-Fi AP 热点并配置固定 IPv4。
import network

# multimedia：固件提供的 RTSP 服务器接口。
import multimedia as mm

# MediaManager：申请媒体缓冲区，并连接 Sensor 与 VENC 硬件模块。
from media.media import MediaManager

# Display：驱动立创开发板上的 ST7701 LCD。
from media.display import Display

# Sensor：配置并启动板载摄像头。
from media.sensor import Sensor

# vencoder 中包含 Encoder、StreamData、ChnAttrStr、通道常量等。
# CanMV 官方示例通常使用 * 导入，本文件沿用该写法以兼容固件定义。
from media.vencoder import *


# =========================================================
# [常量区 1] Wi-Fi AP 热点与固定网络地址
# =========================================================

# AP_SSID 是 Pad 搜索到的热点名称；AP_PASSWORD 是连接密码。
# WPA2 密码至少需要 8 个字符，所以这里使用 8 位数字。
AP_SSID = "K230_AP_EXAMPLE"
AP_PASSWORD = "CHANGE_ME_BEFORE_DEPLOY"

# 2.4 GHz Wi-Fi 常用信道为 1、6、11。
# 固定信道可以减少每次启动结果不一致，信道 6 对手机/Pad 兼容性较好。
AP_CHANNEL = 6

# K230 是热点创建者（AP），因此可以主动固定自己的 IP。
# AP_IP 是 K230 地址，也是 Pad 访问 RTSP 服务时填写的服务器地址。
# AP_NETMASK 为 255.255.255.0，表示 192.168.8.x 位于同一局域网。
# AP_GATEWAY 和 AP_DNS 在本项目中不负责上网，但仍填成 K230 自身地址，
# 让网络配置保持完整。Pad 本身需要手动设置为 192.168.8.2/24。
AP_IP = "192.168.8.1"
AP_NETMASK = "255.255.255.0"
AP_GATEWAY = "192.168.8.1"
AP_DNS = "192.168.8.1"

# CanMV 固件中的网络设备名称：
#   w0：STA 模式，K230 作为客户端去连接别人的热点；
#   w1：AP 模式，K230 自己创建热点；
#   u0：部分固件中的 USB/有线网络设备。
# 本项目使用 AP 模式，所以 RTSP socket 应绑定默认设备 w1。
AP_DEVICE_NAME = "w1"

# 冷启动后先等待 RT-Smart、Wi-Fi 驱动、摄像头供电和时钟稳定。
# 在 CanMV IDE 中手动运行时硬件通常早已启动，所以看不出这个等待的作用；
# 但 main.py 上电自启时如果立即初始化，容易出现偶发黑屏或热点启动失败。
BOOT_STABILIZE_DELAY_S = 5

# 调用 access_point.config() 只是向驱动下达启动命令。
# 驱动真正建立热点需要时间，所以配置后再等待 2 秒。
AP_STARTUP_WAIT_S = 2

# 最长等待 10 秒，直到 ifconfig() 确认热点接口已经取得固定 IP。
# 超时后抛出异常，由最外层监督循环重新启动整套服务。
AP_IP_TIMEOUT_S = 10


# =========================================================
# [常量区 2] RTSP 与视频编码参数
# =========================================================

# RTSP URL 由“协议 + K230 IP + 端口 + 会话名”组成：
#     rtsp://192.168.8.1:8554/test
# 8554 是服务器监听端口，test 是播放器请求的视频会话名称。
RTSP_PORT = 8554
RTSP_SESSION_NAME = "test"

# 当前先保证低延迟和稳定性：
#   640x360：每帧像素较少，编码和无线传输压力较低；
#   20 FPS：滚球运动仍能观察，同时减少码率和缓存。
# 链路稳定后可以再尝试 1280x720，但不要一次同时提高分辨率和帧率。
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
VIDEO_FPS = 20

# H.264 目标码率，单位 kbit/s。
# 1200 kbit/s 对 640x360 足够；码率越高画质通常越好，但 Wi-Fi 堵塞和延迟风险越大。
VIDEO_BITRATE_KBPS = 1200

# GOP 是相邻关键帧之间的最大帧数。
# 当前 10 帧 / 20 FPS = 每 0.5 秒至少出现一次关键帧。
# GOP 越短，Pad 加入直播、开始录像和拖动进度条时恢复画面的速度越快；
# 代价是关键帧数据更大，会略微提高平均码率。
VIDEO_GOP_FRAMES = 10

# 是否在 IDR 关键帧缺少参数集时，主动补上缓存的 SPS/PPS。
# SPS/PPS 告诉解码器分辨率、编码档次和图像参数；缺失时常见报错是
# “non-existing PPS referenced”，表现为录像开头几秒不动，之后才恢复。
# 打开该选项后，即使 Pad 在直播中途开始录像，下一个 IDR 也能独立解码。
REPEAT_H264_PARAMETER_SETS = True

# VENC（Video Encoder，视频编码器）输出缓冲区数量。
# 缓冲区太少时，网络线程还没发完上一帧，编码器就可能没有空间放下一帧。
# 设为 12 是内存占用和抗抖动能力之间的折中。
VENC_OUTPUT_BUFFER_COUNT = 12

# 当编码器时间戳异常时，用固定帧率推算下一帧时间。
# RTSP 发送接口使用毫秒；20 FPS 的理论帧间隔为 1000/20 = 50 ms。
FALLBACK_FRAME_INTERVAL_MS = 1000 // VIDEO_FPS


# =========================================================
# [常量区 3] 自启稳定性与故障恢复
# =========================================================

# 推流线程异常后等待 3 秒，再重建摄像头、编码器和 RTSP。
# 此时热点仍然保留，Pad 一般不需要重新连接 Wi-Fi。
RTSP_RESTART_DELAY_S = 3

# 热点本身异常后等待 3 秒，重建 Wi-Fi AP 和整个 RTSP 链路。
AP_RESTART_DELAY_S = 3

# 主监督循环的检查周期。真正逐帧发送视频的是后台线程。
MAIN_LOOP_SLEEP_S = 1

# 停止服务时最多等待后台线程 50 次，每次 0.1 秒，总计 5 秒。
# 加上上限是为了避免 GetStream 等底层调用异常卡住后，清理流程永远无法结束。
STREAM_STOP_WAIT_LOOPS = 50


# =========================================================
# [常量区 4] LCD 状态显示
# =========================================================

# 立创 K230 开发板配套 LCD 使用 ST7701，横屏分辨率为 800x480。
LCD_WIDTH = 800
LCD_HEIGHT = 480

# 只输出到板载 LCD，不把状态画面同步传给 CanMV IDE。
# 关闭 IDE 图像输出可以减少额外内存和 USB 传输开销。
LCD_OUTPUT_TO_IDE = False

# 三个状态字符串严格对应当前需求。
LCD_STATUS_CREATING = "正在创建热点中"
LCD_STATUS_CONNECTED = "热点已连接"
LCD_STATUS_DISCONNECTED = "热点未连接"

# 状态文字字号。draw_string_advanced() 支持中文字体绘制。
LCD_STATUS_FONT_SIZE = 56

# LCD 状态颜色：
#   创建中：黄色；
#   已连接：绿色；
#   未连接：红色。
LCD_COLOR_CREATING = (255, 190, 0)
LCD_COLOR_CONNECTED = (0, 210, 90)
LCD_COLOR_DISCONNECTED = (235, 65, 65)
LCD_BACKGROUND_COLOR = (18, 22, 28)


class LcdStatusDisplay:
    """
    在 ST7701 LCD 上显示当前热点状态。

    稳定性原则：
        1. LCD 是辅助功能，不能影响核心 RTSP 图传；
        2. 初始化或刷新失败时，只打印串口错误，不向外抛异常；
        3. 只有状态发生变化时才重画，避免无意义地持续占用 CPU；
        4. 只保留一张 800x480 ARGB8888 图像并反复使用。
    """

    def __init__(self):
        self._image = None
        self._initialized = False
        self._current_status = None

    def start(self):
        """
        初始化 LCD 和用于绘图的内存图像。

        返回：
            True：LCD 初始化成功。
            False：LCD 初始化失败；后续图传仍可继续运行。
        """

        if self._initialized:
            return True

        try:
            # 本地 CanMV v1.7 官方示例使用 ARGB8888 作为 OSD 图像格式。
            self._image = image.Image(
                LCD_WIDTH,
                LCD_HEIGHT,
                image.ARGB8888,
            )

            Display.init(
                Display.ST7701,
                width=LCD_WIDTH,
                height=LCD_HEIGHT,
                osd_num=1,
                to_ide=LCD_OUTPUT_TO_IDE,
            )

            self._initialized = True
            print("LCD status display started.")
            return True

        except BaseException as error:
            print("LCD status display start failed:", error)

            # Display.init() 可能只完成了一部分底层初始化。
            # 尝试反初始化，避免残留显示资源影响后续 VENC/MediaManager。
            try:
                Display.deinit()
            except BaseException:
                pass

            self._image = None
            self._initialized = False
            return False

    @staticmethod
    def _status_style(status_text):
        """
        根据状态文字选择颜色和横向位置。

        中文字符通常接近方形，不同状态字符串长度不同，所以分别设置 x，
        让三种文字都大致位于 800x480 屏幕中央。
        """

        if status_text == LCD_STATUS_CONNECTED:
            return LCD_COLOR_CONNECTED, 260

        if status_text == LCD_STATUS_DISCONNECTED:
            return LCD_COLOR_DISCONNECTED, 260

        return LCD_COLOR_CREATING, 200

    def show(self, status_text):
        """
        显示一个状态；相同状态不会重复刷新。
        """

        if not self._initialized or self._image is None:
            return

        if status_text == self._current_status:
            return

        try:
            text_color, text_x = self._status_style(status_text)
            text_y = (LCD_HEIGHT - LCD_STATUS_FONT_SIZE) // 2

            # 先清空上一状态，再绘制深色背景和当前状态文字。
            self._image.clear()
            self._image.draw_rectangle(
                0,
                0,
                LCD_WIDTH,
                LCD_HEIGHT,
                color=LCD_BACKGROUND_COLOR,
                fill=True,
            )
            self._image.draw_string_advanced(
                text_x,
                text_y,
                LCD_STATUS_FONT_SIZE,
                status_text,
                color=text_color,
            )

            Display.show_image(
                self._image,
                0,
                0,
                Display.LAYER_OSD0,
            )
            self._current_status = status_text
            print("LCD status:", status_text)

        except BaseException as error:
            # 显示故障不能中断 Wi-Fi 或 RTSP。
            print("LCD status update failed:", error)

    def stop(self):
        """
        程序最终退出时释放 LCD；正常自动重启 RTSP 时不调用。
        """

        if not self._initialized:
            return

        try:
            Display.deinit()
        except BaseException as error:
            print("LCD status display stop failed:", error)

        self._image = None
        self._initialized = False
        self._current_status = None


def build_rtsp_url(ip_address):
    """
    根据服务器 IP、固定端口和会话名拼出播放器使用的完整 RTSP 地址。

    参数：
        ip_address：K230 热点接口的 IPv4 字符串，例如 "192.168.8.1"。

    返回：
        str：Pad/VLC 可以直接打开的 URL。

    示例：
        rtsp://192.168.8.1:8554/test
    """

    return "rtsp://%s:%d/%s" % (
        ip_address,
        RTSP_PORT,
        RTSP_SESSION_NAME,
    )


def read_ip_address(nic):
    """
    安全读取 CanMV 网络对象当前的 IPv4 地址。

    为什么不直接写 nic.ifconfig()[0]：
        网络驱动启动过程中，ifconfig() 可能暂时返回 None。
        对 None 取下标会触发：
            TypeError: 'NoneType' object isn't subscriptable

    参数：
        nic：network.WLAN 创建的网络接口对象。

    返回：
        str：成功时返回 IPv4 字符串。
        None：网络配置尚未准备好。
    """

    config = nic.ifconfig()

    if config is None:
        return None

    if len(config) == 0:
        return None

    return config[0]


def wait_for_fixed_ap_ip(access_point, timeout_s):
    """
    轮询等待 AP 接口真正取得 AP_IP 指定的固定地址。

    AP 与 STA 的区别：
        STA 是“连接别人热点”的客户端，因此通常用 isconnected() 判断联网。
        AP 是热点本身，并不需要连接另一台路由器，所以这里主要检查 ifconfig()。

    参数：
        access_point：AP 模式 WLAN 对象。
        timeout_s：最多等待的秒数。

    返回：
        str：成功时返回 AP_IP。
        None：超过 timeout_s 仍未获得固定 IP。
    """

    start_time = time.time()

    while time.time() - start_time < timeout_s:
        # 给 CanMV IDE/REPL 一个打断长循环的机会。
        os.exitpoint()

        ip_address = read_ip_address(access_point)
        if ip_address == AP_IP:
            return ip_address

        time.sleep(0.2)

    return None


def select_default_network_device():
    """
    指定固件后续创建 socket/RTSP 服务时默认使用 Wi-Fi AP 设备 w1。

    K230 可能同时存在 w0、w1、u0 等网络接口。如果默认接口选错，
    热点虽然能被搜索到，但 RTSP 服务可能监听在另一个接口上，
    Pad 访问 192.168.8.1:8554 就会连接失败。

    某些旧固件没有 set_default_dev()，因此先用 hasattr() 检查兼容性。
    """

    if not hasattr(network, "set_default_dev"):
        print("network.set_default_dev() is not available, skip.")
        return

    result = network.set_default_dev(AP_DEVICE_NAME)
    if result is False:
        raise RuntimeError(
            "Failed to select default network device: %s"
            % AP_DEVICE_NAME
        )


def start_wifi_ap(status_display=None):
    """
    创建 K230 Wi-Fi 热点，并把热点接口固定为 192.168.8.1。

    执行顺序：
        1. 创建 AP 模式 WLAN 对象；
        2. 设置 SSID、密码和信道；
        3. 写入固定 IPv4 配置；
        4. 等待驱动确认 IP；
        5. 把 w1 选为默认网络设备。

    返回：
        network.WLAN:
            已启动的 AP 对象。RTSP 运行期间必须一直保存它的引用，
            否则对象被垃圾回收后，热点可能意外停止。

    异常：
        RuntimeError：在规定时间内没有取得 AP_IP。
    """

    access_point = network.WLAN(network.AP_IF)

    if status_display is not None:
        status_display.show(LCD_STATUS_CREATING)

    print("Starting Wi-Fi AP...")
    print("SSID:", AP_SSID)
    print("Password: <configure locally>")

    # config() 会把 SSID、密码等参数交给 Wi-Fi 驱动并启动 AP。
    # 新固件支持 channel 参数，旧固件可能因此抛 TypeError；
    # 捕获后去掉 channel 再调用，实现一份代码兼容两个 API 版本。
    try:
        access_point.config(
            ssid=AP_SSID,
            key=AP_PASSWORD,
            channel=AP_CHANNEL,
        )
    except TypeError:
        access_point.config(
            ssid=AP_SSID,
            key=AP_PASSWORD,
        )

    # ifconfig 的四元组依次为：
    # (本机 IP, 子网掩码, 默认网关, DNS)。
    # 这是整个“固定 RTSP 地址”方案的核心配置。
    access_point.ifconfig(
        (
            AP_IP,
            AP_NETMASK,
            AP_GATEWAY,
            AP_DNS,
        )
    )

    time.sleep(AP_STARTUP_WAIT_S)
    ip_address = wait_for_fixed_ap_ip(
        access_point,
        AP_IP_TIMEOUT_S,
    )

    if ip_address is None:
        raise RuntimeError(
            "Wi-Fi AP did not get fixed IP %s"
            % AP_IP
        )

    select_default_network_device()

    print("Wi-Fi AP started.")
    print("AP info:", access_point.info())
    print("AP status:", access_point.status())
    print("AP config:", access_point.ifconfig())
    print("Pad should connect Wi-Fi SSID:", AP_SSID)
    print("Pad should open RTSP URL:")
    print(build_rtsp_url(AP_IP))

    # 热点启动成功不代表 Pad 已接入。
    # 先显示“未连接”，后续监督循环通过 stations 列表实时更新。
    if status_display is not None:
        status_display.show(LCD_STATUS_DISCONNECTED)

    return access_point


def is_wifi_ap_healthy(access_point):
    """
    判断运行中的热点是否仍然健康。

    为什么不能只看 Pad 上是否还显示热点名称：
        Wi-Fi 驱动异常后，Pad 可能短时间仍显示已经保存的 SSID。
        因此这里同时检查 K230 接口是否还持有固定 IP，以及 status()
        是否明确返回 False。任意一步失败，都让外层重建热点。

    返回：
        True：热点接口仍持有 AP_IP，且没有明确报告失败。
        False：IP 丢失、状态失败或检查过程中出现异常。
    """

    try:
        if read_ip_address(access_point) != AP_IP:
            return False

        # 当前固件在 AP 模式返回 True；部分版本可能返回整数状态码。
        # 因此只把“明确等于 False”视为失败，避免把合法的整数 0/1 误判。
        if access_point.status() is False:
            return False

        return True
    except BaseException as error:
        print("Wi-Fi AP health check failed:", error)
        return False


def has_connected_ap_station(access_point):
    """
    判断是否至少有一台 Pad/电脑已经接入 K230 热点。

    AP 模式下不能使用普通 STA 的“是否连上路由器”语义。
    CanMV v1.7 提供 status("stations")，返回当前接入设备的信息列表。

    返回：
        True：至少有一台客户端已关联到 K230 热点。
        False：没有客户端，或者当前固件无法取得客户端信息。
    """

    try:
        stations = access_point.status("stations")

        if stations is None:
            return False

        try:
            return len(stations) > 0
        except TypeError:
            return bool(stations)

    except BaseException:
        # 部分旧版示例在 AP 模式下使用 isconnected() 判断客户端。
        # 仅作为兼容后备；当前 v1.7 固件应优先走 stations 分支。
        try:
            return bool(access_point.isconnected())
        except BaseException:
            return False


def update_lcd_ap_connection_status(access_point, status_display):
    """
    根据 AP 客户端列表更新 LCD，显示“已连接”或“未连接”。
    """

    if status_display is None:
        return

    if has_connected_ap_station(access_point):
        status_display.show(LCD_STATUS_CONNECTED)
    else:
        status_display.show(LCD_STATUS_DISCONNECTED)


def stop_wifi_ap(access_point):
    """
    安全停止 Wi-Fi 热点。

    该函数用于 finally 清理阶段，因此不再向外抛出停止异常。
    否则“停止热点失败”可能覆盖前面真正需要排查的 RTSP 异常。
    """

    if access_point is None:
        return

    try:
        access_point.stop()
    except BaseException as error:
        print("Wi-Fi AP stop failed:", error)


class RecordableRtspServer:
    """
    封装“摄像头 -> H.264 编码器 -> RTSP 服务器”完整媒体链路。

    编码器一次 GetStream() 可能返回多个 pack。多个 pack 可能共同组成
    一个完整 H.264 Access Unit（通常对应一帧图像以及相关参数 NAL）。
    本类先把它们合并，再作为一次 RTSP 视频数据发送。

    这样做比“每个 pack 单独当成一帧发送”更符合 Android 播放器的预期，
    也能减少录像中 SPS/PPS 与关键帧被拆散导致的解析问题。
    """

    def __init__(self, session_name=RTSP_SESSION_NAME, port=RTSP_PORT):
        # RTSP 客户端通过端口和会话名找到这一路视频。
        self.session_name = session_name
        self.port = port

        # 创建 RTSP 服务对象；VENC_CHN_ID_0 表示使用硬件编码通道 0。
        self.rtspserver = mm.rtsp_server()
        self.venc_chn = VENC_CHN_ID_0

        # 以下三个对象在 _init_stream() 中创建，在 _stop_stream() 中释放。
        self.sensor = None
        self.encoder = None
        self.link = None

        # start_stream：控制后台线程 while 循环是否继续运行。
        # runthread_over：后台线程退出后置 True，供主线程等待与健康检查。
        self.start_stream = False
        self.runthread_over = False

        # 后台线程不能直接把异常抛给主线程，所以先保存到该成员变量，
        # 再由主线程调用 check_health() 将它转换为 RuntimeError。
        self._thread_error = None

        # 记录上一帧时间戳，保证发送给 RTSP 的时间单调递增。
        self._last_timestamp_ms = None

        # 缓存最近一次编码器输出的 H.264 SPS/PPS，用于补到后续 IDR 前面。
        self._cached_sps = None
        self._cached_pps = None

    def _call_encoder_with_optional_channel(self, method_name, *args):
        """
        兼容不同 CanMV 固件版本的 Encoder 方法参数。

        某些版本要求：
            encoder.Start(channel)

        另一些版本/示例要求：
            encoder.Start()

        本函数先尝试带通道号调用；如果参数数量不匹配而触发 TypeError，
        再退回不带通道号的形式。Stop、Destroy 等方法也复用这个兼容层。

        参数：
            method_name：要调用的方法名，例如 "Start"。
            *args：除通道号以外的其他参数。

        返回：
            底层 Encoder 方法的原始返回值。
        """

        method = getattr(self.encoder, method_name)

        try:
            return method(self.venc_chn, *args)
        except TypeError:
            return method(*args)

    def _get_stream(self, stream_data):
        """
        从 VENC 取出一批已经编码好的 H.264 数据。

        当前立创开发板 CanMV 固件使用：
            encoder.GetStream(streamData)

        这里不能先尝试 GetStream(channel, streamData)。该固件的 C 扩展
        会把整数 channel 错当成 streamData 对象，进而出现：
            'int' object has no attribute 'pack_cnt'

        stream_data 不是 H.264 字节本身，而是描述编码结果的结构体，
        其中包含 pack_cnt、每个 pack 的地址、长度和时间戳。
        """

        return self.encoder.GetStream(stream_data)

    def _release_stream(self, stream_data):
        """
        把 GetStream() 取得的底层编码缓冲区归还给 VENC。

        GetStream 和 ReleaseStream 必须成对出现。若只取不还，
        VENC_OUTPUT_BUFFER_COUNT 个缓冲区最终会全部占满，推流就会卡死。
        """

        return self.encoder.ReleaseStream(stream_data)

    def _init_stream(self):
        """
        初始化摄像头、媒体缓冲区、模块连接和 H.264 编码器。

        数据方向：
            Sensor 输出 YUV420SP
                -> MediaManager 建立硬件通路
                -> VENC 通道 0 输出 H.264。

        本函数只负责“创建资源”，真正启动数据流由 _start_stream() 完成。
        """

        # 硬件编码器通常要求宽度按 16 字节/像素边界对齐。
        # 640 本身能被 16 整除，因此对齐后仍为 640。
        width = ALIGN_UP(VIDEO_WIDTH, 16)
        height = VIDEO_HEIGHT

        # reset() 探测并初始化摄像头；set_framesize() 配置输出尺寸。
        # YUV420SP 是硬件编码器能够直接接收的像素格式，避免额外颜色转换。
        self.sensor = Sensor()
        self.sensor.reset()
        self.sensor.set_framesize(
            width=width,
            height=height,
            alignment=12,
        )
        self.sensor.set_pixformat(Sensor.YUV420SP)

        self.encoder = Encoder()

        # 预先为编码器输出申请缓冲区。
        # try/except 同样用于兼容“需要通道号”和“不需要通道号”的固件 API。
        try:
            self.encoder.SetOutBufs(
                self.venc_chn,
                VENC_OUTPUT_BUFFER_COUNT,
                width,
                height,
            )
        except TypeError:
            self.encoder.SetOutBufs(
                VENC_OUTPUT_BUFFER_COUNT,
                width,
                height,
            )

        # 把 Sensor 的输出端绑定到 VENC 模块输入端。
        # link 对象必须保存在 self.link 中，不能创建后立即被垃圾回收。
        self.link = MediaManager.link(
            self.sensor.bind_info()["src"],
            (
                VIDEO_ENCODE_MOD_ID,
                VENC_DEV_ID,
                self.venc_chn,
            ),
        )

        # 统一申请并初始化前面配置过的媒体缓冲区。
        MediaManager.init()

        # 创建编码通道属性：
        #   H.264 Baseline：兼容性较好，适合实时传输；
        #   bit_rate：控制平均数据量；
        #   gopLen：控制关键帧间隔；
        #   src/dst_frame_rate：采集和编码输出都使用 VIDEO_FPS。
        encoder_attributes = ChnAttrStr(
            self.encoder.PAYLOAD_TYPE_H264,
            self.encoder.H264_PROFILE_BASELINE,
            width,
            height,
            bit_rate=VIDEO_BITRATE_KBPS,
            gopLen=VIDEO_GOP_FRAMES,
            src_frame_rate=VIDEO_FPS,
            dst_frame_rate=VIDEO_FPS,
        )

        try:
            self.encoder.Create(
                self.venc_chn,
                encoder_attributes,
            )
        except TypeError:
            self.encoder.Create(encoder_attributes)

    def _start_stream(self):
        """
        按“编码器在前、摄像头在后”的顺序启动媒体数据流。

        先让编码器准备好接收数据，再启动 Sensor，能够避免摄像头已经输出
        帧而编码端尚未就绪造成开头丢帧。
        """

        self._call_encoder_with_optional_channel("Start")
        self.sensor.run()

    def _stop_stream(self):
        """
        按初始化的反方向释放媒体资源。

        释放顺序：
            1. 停止 Sensor，不再产生新帧；
            2. 删除 Sensor -> VENC 的硬件连接；
            3. 停止并销毁编码器通道；
            4. 释放 MediaManager 公共缓冲区。

        每一步独立捕获异常，保证前一步失败后仍能继续释放后面的资源。
        这对自动重启很重要，否则残留的编码通道可能导致下一次 Create 失败。
        """

        if self.sensor is not None:
            try:
                self.sensor.stop()
            except BaseException as error:
                print("sensor stop failed:", error)
            self.sensor = None

        if self.link is not None:
            try:
                del self.link
            except BaseException as error:
                print("media link release failed:", error)
            self.link = None

        if self.encoder is not None:
            try:
                self._call_encoder_with_optional_channel("Stop")
            except BaseException as error:
                print("encoder stop failed:", error)

            try:
                self._call_encoder_with_optional_channel("Destroy")
            except BaseException as error:
                print("encoder destroy failed:", error)

            self.encoder = None

        try:
            MediaManager.deinit()
        except BaseException as error:
            print("MediaManager deinit failed:", error)

    def _select_timestamp_ms(self, stream_data):
        """
        取得当前编码帧的 RTSP 毫秒时间戳，并保证它严格递增。

        PTS（Presentation Timestamp）表示这一帧应该在什么时候显示。
        VENC 给出的 PTS 通常是微秒，RTSP 发送接口需要毫秒，所以除以 1000。

        如果底层 PTS 重复或倒退，播放器可能认为时间线损坏，进而无法拖动、
        录像时长异常或画面停顿。因此异常时按固定帧间隔向前补一个时间戳。

        参数：
            stream_data：本次 GetStream() 填充的 StreamData。

        返回：
            int：单调递增的毫秒时间戳。
        """

        timestamp_ms = 0

        if stream_data.pack_cnt > 0:
            timestamp_ms = stream_data.pts[0] // 1000

        if (
            self._last_timestamp_ms is not None
            and timestamp_ms <= self._last_timestamp_ms
        ):
            timestamp_ms = (
                self._last_timestamp_ms
                + FALLBACK_FRAME_INTERVAL_MS
            )

        self._last_timestamp_ms = timestamp_ms
        return timestamp_ms

    def _read_complete_access_unit(self, stream_data):
        """
        把一次 GetStream() 返回的所有 pack 合并为完整 H.264 Access Unit。

        pack 是编码器底层的内存分块，并不一定等于一整帧。
        uctypes.bytearray_at(address, size) 根据“内存地址 + 字节数”
        访问底层数据，再用 bytes() 复制成由 MicroPython 管理的字节串。

        为什么必须复制：
            ReleaseStream() 后底层缓冲区会归还给编码器，原地址可能马上被
            下一帧覆盖。复制后才能安全地在归还缓冲区之前组织和发送数据。

        返回：
            bytes:
                合并后的完整 H.264 Access Unit。
            None:
                本次编码结果没有有效 pack。
        """

        if stream_data.pack_cnt <= 0:
            return None

        pack_list = []

        for pack_index in range(stream_data.pack_cnt):
            pack_data = bytes(
                uctypes.bytearray_at(
                    stream_data.data[pack_index],
                    stream_data.data_size[pack_index],
                )
            )
            pack_list.append(pack_data)

        if len(pack_list) == 1:
            return pack_list[0]

        return b"".join(pack_list)

    @staticmethod
    def _find_annex_b_start_code(data, start_index):
        """
        从 start_index 开始寻找下一个 H.264 Annex-B 起始码。

        Annex-B 用下面两种字节序列分隔 NAL 单元：
            00 00 01
            00 00 00 01

        NAL 起始码后面的第一个字节是 NAL Header，其低 5 位表示 NAL 类型。

        返回：
            (index, length):
                index 是起始码位置，length 是 3 或 4。
            (-1, 0):
                后面已经找不到起始码。
        """

        data_length = len(data)
        index = start_index

        while index + 2 < data_length:
            if data[index] == 0 and data[index + 1] == 0:
                if data[index + 2] == 1:
                    return index, 3

                if (
                    index + 3 < data_length
                    and data[index + 2] == 0
                    and data[index + 3] == 1
                ):
                    return index, 4

            index += 1

        return -1, 0

    def _prepare_recordable_access_unit(self, access_unit):
        """
        缓存 H.264 SPS/PPS，并保证每个 IDR 关键帧都能独立解码。

        本函数关心的 NAL 类型：
            5：IDR 关键帧，解码可以从这里重新开始；
            7：SPS，保存分辨率、Profile、Level 等序列参数；
            8：PPS，保存图像级解码参数。

        Pad 可能在直播已经运行一段时间后才按下录制。如果新 MP4 的第一帧
        引用了录像开始之前发送过的 PPS，而文件里没有保存那份 PPS，就会报：
            non-existing PPS referenced

        处理流程：
            1. 扫描本 Access Unit 中的所有 NAL；
            2. 遇到 SPS/PPS 就更新缓存；
            3. 遇到 IDR 时检查本帧是否已经包含 SPS/PPS；
            4. 若缺少，就把缓存参数集拼到 IDR 前面。

        这样录制端从任意时刻开始，最迟到下一个 IDR 就能获得完整解码起点。
        """

        current_index, current_code_length = (
            self._find_annex_b_start_code(access_unit, 0)
        )

        # 如果找不到 Annex-B 起始码，说明数据格式不是本函数预期的形式。
        # 为避免误改码流，直接原样返回。
        if current_index < 0:
            return access_unit

        has_idr = False
        has_sps = False
        has_pps = False

        while current_index >= 0:
            nal_header_index = current_index + current_code_length

            if nal_header_index >= len(access_unit):
                break

            next_index, next_code_length = (
                self._find_annex_b_start_code(
                    access_unit,
                    nal_header_index + 1,
                )
            )

            if next_index < 0:
                nal_end_index = len(access_unit)
            else:
                nal_end_index = next_index

            nal_type = access_unit[nal_header_index] & 0x1F
            complete_nal = access_unit[
                current_index:nal_end_index
            ]

            if nal_type == 5:
                has_idr = True
            elif nal_type == 7:
                has_sps = True
                self._cached_sps = complete_nal
            elif nal_type == 8:
                has_pps = True
                self._cached_pps = complete_nal

            if next_index < 0:
                break

            current_index = next_index
            current_code_length = next_code_length

        if not REPEAT_H264_PARAMETER_SETS or not has_idr:
            return access_unit

        prefix = b""

        if not has_sps and self._cached_sps is not None:
            prefix += self._cached_sps

        if not has_pps and self._cached_pps is not None:
            prefix += self._cached_pps

        if len(prefix) == 0:
            return access_unit

        return prefix + access_unit

    def _do_rtsp_stream(self):
        """
        后台推流线程：不断取出编码结果并发送给 RTSP 客户端。

        单帧循环：
            GetStream 取得编码描述结构
                -> 复制并合并全部 pack
                -> 为 IDR 检查/补充 SPS、PPS
                -> 计算毫秒时间戳
                -> rtspserver_sendvideodata 发送
                -> ReleaseStream 归还编码缓冲区。

        该函数运行在 _thread 创建的独立线程中。主线程不负责逐帧传输，
        只负责每秒检查该线程是否仍然健康。
        """

        stream_data = StreamData()

        try:
            while self.start_stream:
                os.exitpoint()

                # GetStream 返回 0 表示成功；非 0 表示本次暂时没有有效数据。
                get_result = self._get_stream(stream_data)

                if get_result != 0:
                    continue

                # 无论中间处理或发送是否抛异常，finally 都必须归还缓冲区。
                try:
                    access_unit = self._read_complete_access_unit(
                        stream_data
                    )

                    if access_unit is None:
                        continue

                    access_unit = (
                        self._prepare_recordable_access_unit(
                            access_unit
                        )
                    )

                    timestamp_ms = self._select_timestamp_ms(
                        stream_data
                    )

                    self.rtspserver.rtspserver_sendvideodata(
                        self.session_name,
                        access_unit,
                        len(access_unit),
                        timestamp_ms,
                    )
                finally:
                    self._release_stream(stream_data)

        except KeyboardInterrupt:
            pass
        except BaseException as error:
            # 子线程异常不能直接在主线程中被 except 捕获。
            # 保存异常并把 runthread_over 置位，主监督循环随后会重建 RTSP。
            self._thread_error = error
            print("RTSP stream thread failed:", error)
        finally:
            self.runthread_over = True
            print("RTSP stream thread stopped.")

    def start(self):
        """
        创建并启动 RTSP 会话、编码器、摄像头和后台推流线程。

        关键顺序：
            1. _init_stream() 创建媒体资源；
            2. rtspserver_init() 监听 8554 端口；
            3. createsession() 创建名为 test 的 H.264 会话；
            4. 启动 RTSP 服务、编码器和摄像头；
            5. 创建后台线程执行 _do_rtsp_stream()。

        任一步失败都会进入 except，立即释放已经创建的资源，再把原异常
        继续抛给外层监督循环。
        """

        if self.start_stream:
            return

        # 每次重启都清空上一次运行留下的时间戳、异常和参数集缓存。
        self._last_timestamp_ms = None
        self.runthread_over = False
        self._thread_error = None
        self._cached_sps = None
        self._cached_pps = None

        self._init_stream()

        # 返回 0 表示端口监听成功；非 0 常见原因是端口仍被旧实例占用。
        if self.rtspserver.rtspserver_init(self.port) != 0:
            raise RuntimeError(
                "RTSP server failed to bind port %d"
                % self.port
            )

        try:
            if self.rtspserver.rtspserver_createsession(
                self.session_name,
                mm.multi_media_type.media_h264,
                False,
            ) != 0:
                raise RuntimeError("RTSP session creation failed")

            self.rtspserver.rtspserver_start()
            self._start_stream()

            # 必须先置 True 再启动线程，否则线程进入 while 时会立即退出。
            self.start_stream = True
            _thread.start_new_thread(
                self._do_rtsp_stream,
                (),
            )
        except BaseException:
            # 如果连线程都没有创建成功，就直接标记“线程已经结束”。
            # 这样 stop() 不会白等一个根本不存在的线程。
            self.start_stream = False
            self.runthread_over = True

            try:
                self.rtspserver.rtspserver_deinit()
            except BaseException:
                pass
            self._stop_stream()
            raise

    def check_health(self):
        """
        由主线程检查后台推流线程是否仍然正常。

        旧版本的问题：
            子线程报错后只打印日志，外层 while 仍然一直运行，
            因此热点还在、端口也可能还在，但已经没有视频帧发送。

        当前做法：
            子线程把异常存入 _thread_error，并把 runthread_over 设为 True；
            本函数发现后抛出 RuntimeError；
            run_rtsp_while_ap_alive() 捕获异常、清理并重建媒体链路。

        正常运行时本函数直接返回 None，不会影响推流。
        """

        if not self.start_stream:
            raise RuntimeError("RTSP stream is not marked as running")

        if not self.runthread_over:
            return

        if self._thread_error is not None:
            raise RuntimeError(
                "RTSP stream thread exited: %s"
                % self._thread_error
            )

        raise RuntimeError("RTSP stream thread stopped unexpectedly")

    def stop(self):
        """
        停止后台线程、RTSP 服务以及全部摄像头/编码器资源。

        首先把 start_stream 设为 False，请求后台 while 循环自行结束；
        然后最多等待 5 秒。线程结束后再停 RTSP 和媒体资源，可避免
        子线程仍在访问缓冲区时主线程提前销毁编码器。
        """

        if not self.start_stream:
            return

        self.start_stream = False

        wait_count = 0
        while (
            not self.runthread_over
            and wait_count < STREAM_STOP_WAIT_LOOPS
        ):
            time.sleep(0.1)
            wait_count += 1

        if not self.runthread_over:
            print(
                "Warning: RTSP stream thread did not stop within 5 s."
            )

        try:
            self.rtspserver.rtspserver_stop()
        except BaseException as error:
            print("RTSP stop failed:", error)

        try:
            self.rtspserver.rtspserver_deinit()
        except BaseException as error:
            print("RTSP deinit failed:", error)

        self._stop_stream()
        self.runthread_over = False


def run_rtsp_while_ap_alive(access_point, status_display=None):
    """
    在 Wi-Fi 热点健康期间，持续监督并在需要时重建 RTSP 媒体链路。

    这是“内层监督循环”：
        - 热点正常、RTSP 正常：每秒调用 server.check_health()；
        - 热点正常、RTSP 异常：释放并等待 3 秒，只重建 RTSP；
        - 热点异常：结束本函数，把控制权交回外层去重建 Wi-Fi。

    这样分层的原因：
        RTSP 偶发异常时没有必要让 Pad 断开 Wi-Fi；只有 AP 本身异常时，
        才重建整个热点和网络服务。

    返回：
        None:
            AP 健康检查失败，调用者需要重建 Wi-Fi。

    异常：
        KeyboardInterrupt:
            用户从 CanMV IDE 或串口 REPL 主动停止程序，继续向外抛出。
    """

    while is_wifi_ap_healthy(access_point):
        os.exitpoint()
        server = None
        update_lcd_ap_connection_status(
            access_point,
            status_display,
        )

        try:
            server = RecordableRtspServer()
            server.start()

            print("RTSP server started.")
            print("Fixed Pad RTSP URL:")
            print(build_rtsp_url(AP_IP))

            while is_wifi_ap_healthy(access_point):
                os.exitpoint()
                update_lcd_ap_connection_status(
                    access_point,
                    status_display,
                )
                server.check_health()
                time.sleep(MAIN_LOOP_SLEEP_S)

            print("Wi-Fi AP became unhealthy.")
            if status_display is not None:
                status_display.show(LCD_STATUS_DISCONNECTED)

        except KeyboardInterrupt:
            raise

        except BaseException as error:
            print("RTSP service error:", error)

        finally:
            if server is not None:
                try:
                    server.stop()
                except BaseException as error:
                    print("RTSP cleanup failed:", error)

            # 主动回收已经失去引用的 MicroPython 对象，降低多次重启后的
            # 内存碎片和内存不足风险。
            gc.collect()

        if not is_wifi_ap_healthy(access_point):
            return

        print(
            "Restart RTSP after %d seconds."
            % RTSP_RESTART_DELAY_S
        )
        time.sleep(RTSP_RESTART_DELAY_S)


def run_service_forever(status_display=None):
    """
    监督完整服务：Wi-Fi AP + 摄像头 + H.264 编码器 + RTSP。

    这是“外层监督循环”，负责更高一级的恢复：
        RTSP 故障：
            由内层循环处理，保留 Wi-Fi，只重建媒体链路。
        Wi-Fi 故障：
            停止当前 AP，等待后重新创建热点和 RTSP。
        上电初始化故障：
            打印原因、释放已创建资源、等待后重试，
            避免 main.py 因一次偶发错误退出后再也不工作。

    access_point 每轮先设为 None，是为了保证即使 start_wifi_ap()
    在创建中途抛异常，finally 仍然可以安全调用 stop_wifi_ap(None)。
    """

    while True:
        os.exitpoint()
        access_point = None

        try:
            if status_display is not None:
                status_display.show(LCD_STATUS_CREATING)

            access_point = start_wifi_ap(status_display)
            run_rtsp_while_ap_alive(
                access_point,
                status_display,
            )

        except KeyboardInterrupt:
            print("User stopped program.")
            break

        except BaseException as error:
            print("Complete service error:", error)
            if status_display is not None:
                status_display.show(LCD_STATUS_DISCONNECTED)

        finally:
            stop_wifi_ap(access_point)
            gc.collect()

        print(
            "Restart Wi-Fi AP and RTSP after %d seconds."
            % AP_RESTART_DELAY_S
        )
        time.sleep(AP_RESTART_DELAY_S)


def main():
    """
    程序总入口，也是 /sdcard/main.py 上电自启后首先执行的函数。

    启动顺序：
        1. 开启 CanMV exitpoint，使 IDE 停止按钮和 KeyboardInterrupt 生效；
        2. 冷启动等待 5 秒，让底层硬件稳定；
        3. 进入 run_service_forever()；
        4. 外层循环启动固定 IP 热点；
        5. 内层循环启动摄像头、编码器和 RTSP 推流。

    为什么 main() 本身很短：
        入口只负责编排，不放具体网络和媒体细节。这样阅读时可以先看主流程，
        再按函数逐层进入；调试时也能明确错误属于启动、网络还是媒体部分。
    """

    status_display = LcdStatusDisplay()

    # 开启普通运行阶段的退出检查。
    os.exitpoint(os.EXITPOINT_ENABLE)

    print("K230 fixed-IP AP RTSP transmitter.")
    print(
        "Boot stabilize delay: %d s"
        % BOOT_STABILIZE_DELAY_S
    )

    # LCD 是旁路状态提示。即使 start() 返回 False，核心图传仍继续启动。
    status_display.start()
    status_display.show(LCD_STATUS_CREATING)

    time.sleep(BOOT_STABILIZE_DELAY_S)

    try:
        run_service_forever(status_display)
    finally:
        status_display.stop()

        # 程序退出前切换到允许休眠阶段的 exitpoint 状态。
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)


# 只有直接运行本文件（包括上电执行 /sdcard/main.py）时才进入 main()。
# 如果以后把它作为模块 import，下面的 main() 不会自动重复执行。
if __name__ == "__main__":
    main()
