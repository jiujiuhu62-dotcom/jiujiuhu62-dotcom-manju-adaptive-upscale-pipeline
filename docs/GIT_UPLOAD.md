# 手动 Git 上传流程

以下是供手动复现的命令文本，不会因阅读本文件而执行。仓库命名采用“作者 GitHub 用户名-manju-adaptive-upscale-pipeline”，可选 Public 便于面试查看。若从源码新建独立仓库，按以下流程操作；已关联的仓库无需重复初始化、关联远程。网页创建空仓库时不预先添加 README、忽略规则或协议。

1. 先在终端进入本预览目录，确认文件列表：

```powershell
Get-ChildItem -Force
```

2. 初始化独立仓库：

```powershell
git init -b main
```

3. 交互输入当前仓库提交署名：

```powershell
git config user.name (Read-Host '提交署名')
```

4. 交互输入邮箱，可使用账号提供的隐私邮箱：

```powershell
git config user.email (Read-Host '提交邮箱')
```

5. 查看文件与忽略状态：

```powershell
git status --short --ignored
```

6. 仅在确认目录正确后暂存：

```powershell
git add -- .
```

7. 审核实际暂存的文件名：

```powershell
git diff --cached --name-only
```

8. 审核变更量，核对只有代码、文档、模板与两张示例图：

```powershell
git diff --cached --stat
```

9. 检查空白/冲突标记等基础问题：

```powershell
git diff --cached --check
```

10. 审核无误后提交：

```powershell
git commit -m "feat: add adaptive ComfyUI upscale pipeline"
```

11. 关联你自己创建的空仓库：

```powershell
git remote add origin (Read-Host '你的空仓库 HTTPS 地址')
```

12. 核对远程地址：

```powershell
git remote -v
```

13. 最后由你手动推送；认证在本机完成：

```powershell
git push -u origin main
```

不使用强制推送。已有 origin 或远端已有历史时先检查，不重复添加或覆盖。不要将密码、token 或私钥写进命令文件。大媒体可另行放 GitHub Release 或你选择的文件托管位置，再更新 README 链接；本次没有上传媒体或建立链接。LFS 只是可选存储方案，不作为提交 GB 级素材的理由。
