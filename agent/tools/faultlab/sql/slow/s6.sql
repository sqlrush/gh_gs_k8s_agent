-- S6 窗口函数 + 大排序落盘(5 张表)。目标:3 分钟左右跑完。
--
-- 成因:四个窗口函数各要一次排序,与 order_items 关联后行数翻三倍,而会话
-- `work_mem` 只有 2MB、排序键里还带着宽字符串列 → 每一次排序都落临时文件、反复归并。
--
-- **为什么排序键要带宽列:** 第一版只排 amount / order_date 这些窄列,240k 行 2.8 秒就跑完了
-- —— 窄行在小内存里也能排不少,归并次数少。把 tracking / note 这类字符串放进排序键和输出里,
-- 每行变宽十几倍,落盘量和归并趟数才真正上来。
--
-- **work_mem 不要再往下调。** 调到 64kB 时临时文件多到把宿主机的文件表打满,
-- og5 容器被带着重启过一次(2026-10-17)。2MB 已经足够产生 external merge。
--
-- 看证据:`EXPLAIN ANALYZE` 里的 `Sort Method: external merge  Disk: … kB`。
-- 现实里的样子:分组排名类报表,数据一多就把临时空间打满。
SELECT count(*) AS n, max(x.rn) AS max_rn, max(x.rsum) AS max_rsum, min(x.first_trk) AS trk
  FROM (
    SELECT o.id, o.amount, c.grade, r.zone, s.tracking, i.note,
           row_number() OVER (PARTITION BY c.grade ORDER BY o.amount DESC, s.tracking) AS rn,
           sum(o.amount) OVER (PARTITION BY r.zone ORDER BY o.order_date, s.tracking) AS rsum,
           rank()        OVER (ORDER BY i.note, o.amount) AS rk,
           first_value(s.tracking) OVER (PARTITION BY c.channel_id ORDER BY i.note) AS first_trk
      FROM (SELECT * FROM fl.orders WHERE id <= {cap}) o
      JOIN fl.customers    c ON c.id = o.cust_id
      JOIN fl.regions      r ON r.id = o.region_id
      JOIN fl.shipments    s ON s.order_id = o.id
      JOIN fl.order_items  i ON i.order_id = o.id
  ) x
