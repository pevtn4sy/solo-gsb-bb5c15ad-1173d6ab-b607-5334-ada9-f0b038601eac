"""Williams 平衡拉丁方序列生成。

Williams 设计是一种平衡一阶残留效应（carryover）的交叉实验设计：
每个条件在每个位置上出现次数相等，且每个有向相邻条件对
（ordered adjacent pair）出现的次数也相等。
"""

from __future__ import annotations


def group_size(n: int) -> int:
    """n 个条件时，一组完整 Williams 序列包含的序列条数。

    偶数条件为 n 条，奇数条件为 2n 条。
    """
    if n < 2:
        raise ValueError("at least two conditions are required")
    return n if n % 2 == 0 else 2 * n


def williams_sequences(n: int) -> list[list[int]]:
    """生成 n 个条件的 Williams 平衡序列。

    返回值为序列列表，每条序列是 ``range(n)`` 的一个排列，
    元素为条件的下标。完整序列组满足：

    * 偶数 n：n 条序列，每个条件在每个位置恰好出现 1 次，
      每个有向相邻条件对恰好出现 1 次；
    * 奇数 n：2n 条序列，每个条件在每个位置恰好出现 2 次，
      每个有向相邻条件对恰好出现 2 次。
    """
    if n < 2:
        raise ValueError("at least two conditions are required")

    # 第一行：0, n-1, 1, n-2, 2, n-3, ...（从两端交替取值）
    first_row: list[int] = []
    for i in range(n):
        if i % 2 == 0:
            first_row.append(i // 2)
        else:
            first_row.append(n - 1 - i // 2)

    # 其余各行：第一行循环加 1（模 n）
    rows = [[(value + r) % n for value in first_row] for r in range(n)]

    if n % 2 == 1:
        # 奇数阶需要再拼接镜像（逆序）方阵，
        # 才能使每个有向相邻对都被覆盖且次数相等
        rows += [list(reversed(row)) for row in rows]

    return rows
