# 配套部署模板

与上一级《Ubuntu24.04_RTX6000D_Conda_Kingbase迁移部署指南_20261020.md》配合使用。

这些是文档附件，不会改变当前程序，也没有在Ubuntu上安装或启动。

1. 目标机将本目录内容复制到`/home/adminroot/smart-help/deploy/`。
2. `web.env.example`→`web.env`，`model.env.example`→`model.env`；填写真实连接和源业务密钥，权限600。
3. `gpu_wsgi.py.example`→`gpu_wsgi.py`，`wait_models.py.example`→`wait_models.py`。
4. 先实施主文档列出的Linux源码适配、恢复新空Kingbase库、实测两个GPU端点。
5. 核对用户名、组名、Conda路径及实际家目录；不能照抄不符合目标机的绝对路径。
6. 两个`.service.example`去掉`.example`后安装到`/etc/systemd/system/`；先`systemd-analyze verify`，再daemon-reload/start，验证后enable。
7. Nginx模板作为新站点安装并启用，先检查已有站点及8080端口。不要覆盖其他站点配置。

`gpu_wsgi`工厂显式加载到cuda:0并复用原`serve_local_models.py`端点。不使用`--preload`，只启动一个GPU worker。Web服务保持单worker、两个线程作为初始部署；是否扩容需实际并发验收。

主文档已只读确认源政策库`policy_db`、账号库`smart_help_auth`均为KingbaseES V008R006C008B0014、Oracle兼容模式、UTF8/C/C。目标保留该模式，不能因使用psycopg2就改成PG兼容模式。模板中的目标`*_mig20261020`是恢复新空库示例，不是要求原地改名或覆盖源库。

未填写CHANGE_ME、未完成Linux适配或未对账时，不得直接上线。数据库安装包与许可证不包含在这些模板内。
