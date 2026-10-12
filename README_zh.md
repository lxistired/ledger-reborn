# Ledger Reborn

## 开源重建计划 #000001：从桌面会计开始，下一站个人报税

**能实际记账、开票、登记收付款、管理库存和对账。数据留在本机，代码公开维护。**

维护者：[lxistired](https://github.com/lxistired) · [下载安装包](https://github.com/lxistired/ledger-reborn/releases) · [路线图](ROADMAP.md) · [英文说明](README.md)

我们用 [REA](https://github.com/morluto/rea) 分析了 QuickBooks Desktop for Mac，并独立实现了会计引擎和界面。这个应用实际保存 SQLite 数据、生成复式分录并计算报表。项目计划长期公开维护，个人报税是系列的下一项计划。

当前日期版本为 `2026.10.12-preview.1`。这是预览版，尚未实现全部 QuickBooks 功能，也未完成全页像素和原版完整交互验收。项目与 Intuit、REA 均无隶属或背书关系。

## 安装与开始使用

在 [Releases](https://github.com/lxistired/ledger-reborn/releases) 选择对应系统和架构。桌面包内含 Python 运行时，打开独立应用窗口。Mac 使用 DMG；Windows 提供便携 ZIP，以及成功构建时附带的安装程序。首版未提供 Apple Developer ID 签名、公证或 Windows 发布者签名。

源码版需要 Python 3.11 或更新版本：

```sh
python server.py
```

浏览器打开 `http://127.0.0.1:8780`。My Company 是空白账本；Synthetic Test Company 是独立的合成测试账本，加载它不会覆盖 My Company。已实现的本地工作流不需要 Intuit 账号、银行凭据或订阅。

已实现：复式记账、凭证冲销、客户/供应商、发票/账单、部分收付款、库存成本、CSV 银行导入/匹配/分类/对账、财务报表、项目和预算、关账及备份。范围为本机、单用户、USD。

尚未实现工资服务、税务申报、在线银行、实际支付、多币种、完整 QuickBooks 公司文件迁移、销售税、贷项、库存退货和负库存。现金报表为管理收付口径。详细政策见 [ENGINE_README.md](ENGINE_README.md)。

## 可复现与长期维护

```sh
python -m unittest discover -s tests -v
python tests/http_smoke.py
```

公开测试使用独立冻结的合成数据。原版官方样例报表的本地对照另记在 [案例说明](docs/CASE_STUDY.md)，原版数据库、图标、原始截图、二进制和反编译原文不随本项目发布。

项目以测试、版本发布、公开 issue 和 [路线图](ROADMAP.md) 持续推进。**#000002 个人报税**目前属于计划，先验证按税年的计算，再考虑申报接口；首版没有报税模块。

[关注 lxistired](https://github.com/lxistired)，跟进后续开源重建项目。欢迎 [Star 仓库](https://github.com/lxistired/ledger-reborn)、提交可以复现的 bug 和具体业务场景。

独立编写的代码采用 MIT。第三方运行时保留各自许可证。REA 是本次研究使用的工具；本应用运行时无需安装 REA。
