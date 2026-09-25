import math

def wave_func(x, width, center_y, amp, freq):
    """复合波：基频正弦 + 半幅倍频余弦。

    参数（自定义函数统一约定）：
    x        当前 x 坐标（0 ~ width，像素）
    width    画布宽度（像素）
    center_y 画布垂直中心（像素）
    amp      基波振幅（像素）
    freq     基频（画布内周期数）

    返回：屏幕 y 坐标（像素，向下为正）
    """
    normalized_x = x / width
    y = center_y + amp * (math.sin(freq * 2 * math.pi * normalized_x) +
                          0.5 * math.cos(freq * 4 * math.pi * normalized_x))
    return y
