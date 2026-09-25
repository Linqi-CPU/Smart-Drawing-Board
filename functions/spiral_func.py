import math

def spiral_func(x, width, center_y, amp, freq):
    """螺旋线：半径随 x 增长，相位随 freq 旋转。

    参数（自定义函数统一约定）：
    x        当前 x 坐标（0 ~ width，像素）
    width    画布宽度（像素）
    center_y 画布垂直中心（像素）
    amp      最大半径（像素）
    freq     螺旋圈数

    返回：屏幕 y 坐标（像素，向下为正）
    """
    normalized_x = x / width
    angle = freq * 2 * math.pi * normalized_x
    radius = amp * normalized_x
    y = center_y + radius * math.sin(angle)
    return y
