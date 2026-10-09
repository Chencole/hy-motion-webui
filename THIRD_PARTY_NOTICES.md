# 第三方说明

本项目是独立界面封装，非模型作者的官方网页产品。

模型来源：https://github.com/Tencent-Hunyuan/HY-Motion-1.0

**Powered by Tencent HY.** 腾讯 HY-Motion 的 Community License 包含地域和使用条件；请阅读原项目许可。

仓库不包含模型权重、原模型源码副本、原版预览资源或用户生成文件。`deploy/windows-gpu/backend` 包含社区编写的分阶段调用适配：CPU/逐层 CUDA 文本编码和 CPU/CUDA 动作生成，调用另行安装的官方模型源码与权重，不修改官方模型架构。
网页调用 `HYMotion/src/hy_motion_cpu/cli.py`；安装步骤由用户单独执行，不会在打开网页时自动更改模型环境。社区适配不是模型作者官方发布的 CPU/GPU 分发版。

本仓库 MIT 许可仅覆盖原创网页和参数调用代码，不授予额外模型权利。已有模型、推理代码、生成产物和其许可仍以对应原项目为准。
