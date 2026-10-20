# 开发与发布约定

- 本仓库是工具功能的源码与网页测试入口。新增功能先在此开发和测试，通过验收后才同步到 gis-toolbox 封装。
- 修改前保留可回退的 Git 标签或外部校验备份，更新 CHANGELOG.md；不覆盖旧标签和发布附件。
- 保持网页独立部署，不引用本机绝对路径或其他仓库；不恢复 NDVI、GEE 或已回退功能。
- 桌面同步使用 gis-toolbox/scripts/sync_web_tools.py 的明确文件映射，记录源提交并运行检查；不把桌面容器依赖加入网页 requirements。
