"""claude-sessions 后端包：按职责拆分的实现。

core      Monitor：会话注册表、轮询扫描、远程消息/队列、审批协调、事件分发
hosted    Hosted：常驻 claude 进程（stream-json 双向）
session   Session：单个转录的累计状态与对外投影
transcript 转录行解析（对话消息提取）
http_api  HTTP/SSE 层
"""
