# 部署与节点说明

## 内置执行链

| 节点 | 作用 |
| --- | --- |
| LoadImage | 读取上传后的低清输入 |
| UpscaleModelLoader | 选择本机实际存在的超分权重 |
| ImageUpscaleWithModel | 按模型固有倍率执行 |
| ImageScale | Lanczos 对齐目标宽高 |
| SaveImage | 保存本地 PNG |

网页模板仅负责单张图。目录扫描、尺寸路由、异常隔离、续跑和报告属于 Python 层。模型与输入为通用示例，使用时在本机选择，不包含测试素材真实文件名。

## MCP

运行 `python mcp_server.py --workspace .` 可启动 stdio 服务；协议版本 2024-11-05。客户端需设置工作目录为项目根目录。没有固定机器路径的通用客户端配置；本项目的 mcp_client.py 直接组织本机服务子进程。

只暴露 get_status、degrade_image、process_batch，MCP 返回状态和本地路径，不返回图像字节。ComfyUI HTTP 是执行接口，与 MCP 工具协议分层。

## 可选服务管理器

local_runtime.py 可管理本次创建的服务，不关闭已存在的服务。需要时手动复制 project.example.json 为 project.json，在本机填写安装位置和已核实权重哈希。project.json 不提交。普通演示优先使用已启动的 ComfyUI，无需管理器。

## 续跑与失败

模型标记留空时不复用 AI 输出。可用 PowerShell 的 Get-FileHash 在自己的模型目录中计算 SHA256，然后传给 --model-revision。修改参数、输入或已有产物后必须重新核验。AI 失败记录为失败，不自动替换成插值。提交超时时，服务端可能已接收任务，先检查 ComfyUI 队列再决定重试。
