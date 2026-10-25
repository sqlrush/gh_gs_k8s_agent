# grmp_middleware —— GRMP 协议兼容中间件（本地测试用）

客户（GRMP，高斯风险管控平台）是一个**白名单 SQL 执行网关**：skill 不能
随便发 SQL，只能执行预先注册好的脚本。这个目录是它的本地等价物，
让 skill 能在开发机上按客户环境的真实约束跑起来。

> ⚠️ **只能跑在开发机上。** 详见下面「安全边界」。

## 为什么要造这个

客户环境有两条硬约束，本地不复现就测不出来：

1. **只执行预注册脚本。** 没有脚本管理接口 —— 「由于安全原因，目前脚本仅能
   通过版本 dml 带出」。所以 explain 这类「用户临时给一条 SQL」的能力在客户
   那边根本递不进去，本地必须能重现这个失败，而不是本地跑得欢、上线才发现。
2. **所有值都是字符串。** 参数和结果都经 JSON 字符串往返，`bool("f")` 是
   `True`、`int("3704.0")` 抛异常、NULL 变成空串 —— 这三类坑只在字符串形态下
   出现。本地若拿到原生 int/bool，本地写出的解析代码到客户环境会全部失效。

## 目录内容

| 文件 | 作用 |
|---|---|
| `grmp_mock/` | 协议兼容的 HTTP 服务：两个接口、两种响应信封、MyBatis 分页对象 |
| `grmp_register.py` | 发布期注册工具，兼导出客户格式的 INSERT DML |
| `grmp_scenarios.py` | 四个业务场景验证（动态性能视图、大表取数、带绑定变量的执行计划、加索引） |
| `grmp_demo.sh` | 十个协议行为的演示脚本 |
| `grmp_gen_memanalyze.py` | memanalyze 的按实例脚本生成器 |

## 用法

```bash
export GRMP_AUTH_TOKEN=<任意 32 位十六进制串>   # 本地自己定，不是客户的令牌

# 1. 把 scripts/registry/ 里的脚本注册进本地白名单
python3 -m grmp_middleware.grmp_register --db ~/.gdaa/grmp/script_config.db

# 2. 起服务（只监听 127.0.0.1）
python3 -m grmp_middleware.grmp_mock --port 8769

# 3. skill 用 driver: grmp 的连接访问它，代码与客户环境完全相同
```

导出交付 DML：

```bash
python3 -m grmp_middleware.grmp_register --dml-out release.sql --user <真实工号>
```

导出会**拒绝**带上没有任何 skill 调用的脚本 —— 每一条 INSERT 都要灌进客户
生产库，客户得为它走变更评审。确实要带就加 `--include-unused`。

## 安全边界

**这个中间件刻意做成文本替换而不是绑定变量**，因为客户的中间件就是这么做的
（`ORDER BY $1` 在那边会静默按常量排序）。行为不复刻，本地就测不出真实风险。

代价是它自身带注入面。因此：

- **只监听 127.0.0.1**，启动时在 stderr 打横幅说明
- **只执行预注册脚本**，不接受任意 SQL
- 会话默认只读
- 参数做严格类型校验；String 型参数在注册时被标注风险（见 `--dml-out` 报告）

**绝不能部署到开发机以外的任何地方。** 它不是产品组件，是测试替身。

## 与 `common/grmp/` 的分工

别混淆：

- `common/grmp/` —— **skill 侧**的运行时层（协议值解析、占位符渲染、脚本仓库、
  两个 Runner）。它要跟着 skill 一起交付到客户环境。
- `grmp_middleware/` —— **服务端**替身。只在开发机上跑，不交付。
