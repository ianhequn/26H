"""最小回归测试：确保两张样图都能检测并且左右符号正确。"""

from pathlib import Path

import cv2

from detector import detect_green_rail_and_ball


def main():
    cases = [
        ("aaa_000002_0.60s.jpg", -1),
        ("aaa_000137_41.10s.jpg", 1),
    ]

    for filename, expected_dx_sign in cases:
        path = Path("test_images") / filename
        frame = cv2.imread(str(path))
        assert frame is not None, "无法读取 %s" % path

        for label, test_frame in [
            ("original", frame),
            ("640x480", cv2.resize(frame, (640, 480))),
        ]:
            result, _, _ = detect_green_rail_and_ball(test_frame)
            assert result["valid"], (
                "%s (%s) 未检测到完整目标" % (filename, label)
            )

            dx, _ = result["error"]
            assert dx * expected_dx_sign > 0, (
                "%s (%s) 的 dx 方向错误: %d"
                % (filename, label, dx)
            )
            print(
                "%s %-8s PASS dx=%d"
                % (filename, label, dx)
            )

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
