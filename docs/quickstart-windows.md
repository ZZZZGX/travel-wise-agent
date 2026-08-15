# 三步跑起来（Windows）

全程在 **cmd 窗口**里做。不要双击 .bat 去猜结果——双击那个窗口即使不闪退，出错信息也不好复制。

---

## 第 0 步：打开 cmd 并进到项目目录

按 `Win + R`，输入 `cmd`，回车。然后：

```bat
cd /d D:\Micorosoft下载\10\travelwise-agent-v0.5.4-envfix\travelwise-agent
```

`cd /d` 的 `/d` 不能省——跨盘符（从 C: 跳到 D:）时没有它是不会切换的。

确认位置对不对：

```bat
dir smoke.bat
```

能看到这个文件就对了。

---

## 第 1 步：告诉它用哪个 Python

```bat
set PY=E:\1comfyui\ComfyUI-aki-v3\ComfyUI-aki-v3\python\python.exe
```

这个设置**只在当前这个 cmd 窗口有效**。窗口一关就没了，下次开新窗口要重新设一遍。

验证一下：

```bat
"%PY%" --version
```

应该打印出 Python 版本号。如果报"不是内部或外部命令"，就是路径写错了。

---

## 第 2 步：整理 .env

先看它要改什么（**不会动文件**）：

```bat
"%PY%" scripts\fix_env.py
```

确认没问题后真正写入（**会先备份成 `.env.bak`**）：

```bat
"%PY%" scripts\fix_env.py --write
```

它做四件事：

1. 同名键重复时合并成一行（保留有值的那个）；
2. `TRAVELWISE_FLIGHT_PROVIDER` 改成 `http`（否则跑的是假数据）；
3. `TRAVELWISE_LLM_PROVIDER` 若还是 `scripted` 但你已填 Key，改成 `openai`；
4. 备用 AppCode 留空时，自动填成与主 AppCode 相同。

最后会列出还空着的必填项。如果提示还缺东西，用**记事本或 VS Code** 打开 `.env` 填上，存盘时选 **UTF-8**。

---

## 第 3 步：跑测试

先跑不花钱的部分：

```bat
smoke.bat
```

或者不用 .bat，直接：

```bat
"%PY%" scripts\smoke_full_flow.py --stage 2
```

阶段 0~2 全绿之后，再跑完整链路：

```bat
smoke.bat 5
```

或者：

```bat
"%PY%" scripts\smoke_full_flow.py --stage 5
```

阶段 3 开始会花钱，每一步都会先报价并问你 y/N。

---

## 常见问题

**`smoke.bat` 双击后窗口一闪就没了**
现在版本末尾加了 `pause`，双击会停住。但仍建议在 cmd 里跑，方便复制错误信息。

**`'xxx' is not recognized as an internal or external command`**
`::` 只有写在 .bat 文件的行首才是注释。文档里那些 `:: 说明文字` 不要跟着命令一起粘贴到 cmd。

**`退出码 9009`**
Windows 的"找不到这个程序"。要么 `PY` 没设，要么路径写错了。回第 1 步验证。

**中文显示成乱码**
`smoke.bat` 里已有 `chcp 65001`。手工跑 python 命令时可以先执行一次 `chcp 65001`。

**改坏了想退回去**
`fix_env.py --write` 会备份成 `.env.bak`，直接复制回来：

```bat
copy /y .env.bak .env
```
