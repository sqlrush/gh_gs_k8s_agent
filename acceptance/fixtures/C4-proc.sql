-- 验收题 C4 的夹具(虚构)。跑题脚本以 fl_app 在验收库的模式 fl 里执行本文件,C4 答完执行文末注释里的两句删掉。
-- 过程**不会被调用**,只供 gaussdb-proctune / gaussdb-procinfo 静态分析;问题点与真值见 truth.yaml(过程体里不写提示)。
CREATE TABLE IF NOT EXISTS fl.accept_err_log (
  acct_id int,
  err_at  timestamp DEFAULT now(),
  err_msg text
);

CREATE OR REPLACE PROCEDURE fl.p_accept_adjust(p_from int, p_to int, p_delta numeric)
AS
DECLARE
  v_sql text;
BEGIN
  FOR i IN p_from .. p_to LOOP
    BEGIN
      v_sql := 'UPDATE fl.acct SET bal = bal + ' || p_delta || ' WHERE id = ' || i;
      EXECUTE IMMEDIATE v_sql;
    EXCEPTION
      WHEN OTHERS THEN
        INSERT INTO fl.accept_err_log(acct_id, err_msg) VALUES (i, SQLERRM);
    END;
    COMMIT;
  END LOOP;
END;
/

-- 收尾:
--   DROP PROCEDURE IF EXISTS fl.p_accept_adjust;
--   DROP TABLE IF EXISTS fl.accept_err_log;
