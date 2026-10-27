-- 季度报表下线后的清理
DROP INDEX cbst.trans_journal_rpt_idx;

-- 新报表:9 月各账户发生额(cbst.trans_journal 按 trans_date 按月分区)
SELECT acct_no, sum(amount) AS total
  FROM cbst.trans_journal
 WHERE to_char(trans_date, 'YYYYMM') = '202509'
 GROUP BY acct_no;

-- 解冻全部冻结账户
UPDATE cbst.acct_balance SET frozen = 0 WHERE frozen = 1;
