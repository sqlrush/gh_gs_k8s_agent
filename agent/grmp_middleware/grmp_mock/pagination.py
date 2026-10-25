"""MyBatis PageHelper 的 PageInfo 序列化结果（全 18 字段）。

接口一响应的 result 是一个完整分页对象，比接口文档参数表多出 16 个字段。
字段名、组合与 navigatePages:8 这个默认值可以确定其来源是 PageHelper，
因此这里直接照搬 PageInfo 的算法，而不是自己设计一套等价逻辑 ——
自己设计的话，边界取值（空页的 startRow、无相邻页的 prePage）必然对不上。

三个容易踩的点，都在下面的实现里：
  1. offset 语义是「第几页」1-based，不是行偏移量
  2. navigatepageNums 第二个 p 小写（PageHelper 原样输出，不符合驼峰规范）
  3. prePage/nextPage 在边界处是 0 而不是 null
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

# PageHelper 默认值，客户响应中即为 8
NAVIGATE_PAGES = 8


def _total_pages(total: int, page_size: int) -> int:
    """PageHelper Page.setTotal 的算法。page_size <= 0 时页数为 0。"""
    if page_size <= 0:
        return 0
    return total // page_size + (0 if total % page_size == 0 else 1)


def _navigate_page_nums(page_num: int, pages: int, navigate_pages: int) -> List[int]:
    """PageHelper PageInfo.calcNavigatepageNums 的算法。

    总页数不超过导航页数时列全部页码；否则以当前页为中心取窗口，
    并在触碰首尾时把窗口整体贴边（而不是截断成不足 navigate_pages 个）。
    """
    if pages <= navigate_pages:
        return list(range(1, pages + 1))

    half = navigate_pages // 2
    start = page_num - half
    end = page_num + half
    if start < 1:
        start = 1
    elif end > pages:
        start = pages - navigate_pages + 1
    return list(range(start, start + navigate_pages))


def paginate(
    items: Sequence[Any],
    page_num: int,
    page_size: int,
    navigate_pages: int = NAVIGATE_PAGES,
) -> Dict[str, Any]:
    """把全量 items 按页码切片，返回 PageInfo 全 18 字段。

    items 不被修改；返回的 list 是新的切片。
    """
    total = len(items)
    pages = _total_pages(total, page_size)

    start_index = (page_num - 1) * page_size
    page_items = list(items[start_index:start_index + page_size])
    size = len(page_items)

    # 空页时 startRow/endRow 均为 0（PageHelper 的 size == 0 分支），
    # 否则 startRow 为该页首行在全集中的 1-based 序号
    if size == 0:
        start_row = 0
        end_row = 0
    else:
        start_row = start_index + 1
        end_row = start_row - 1 + size

    nav = _navigate_page_nums(page_num, pages, navigate_pages)

    # prePage/nextPage 的 Java 字段是 int，无相邻页时保持默认值 0 而非 null。
    # 客户端不能用「非空」判断有无相邻页，要用 hasPreviousPage/hasNextPage。
    pre_page = page_num - 1 if (nav and page_num > 1) else 0
    next_page = page_num + 1 if (nav and page_num < pages) else 0

    return {
        "total": total,
        "list": page_items,
        "pageNum": page_num,
        "pageSize": page_size,
        "size": size,
        "startRow": start_row,
        "endRow": end_row,
        "pages": pages,
        "prePage": pre_page,
        "nextPage": next_page,
        "isFirstPage": page_num == 1,
        "isLastPage": page_num == pages or pages == 0,
        "hasPreviousPage": page_num > 1,
        "hasNextPage": page_num < pages,
        "navigatePages": navigate_pages,
        # 拼写照抄 PageHelper：第二个 p 小写。写成 navigatePageNums 会让
        # 客户端取不到导航页码，且不报错。
        "navigatepageNums": nav,
        "navigateFirstPage": nav[0] if nav else 0,
        "navigateLastPage": nav[-1] if nav else 0,
    }
