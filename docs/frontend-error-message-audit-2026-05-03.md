# 前端中文错误弹窗治理（2026-05-03）

## 目标

- 所有接口报错优先展示后端真实原因，不再只显示“操作失败”“保存失败”“加载失败”。
- 所有弹窗文案必须是中文；后端返回英文常见错误时，由前端做准确中文转换。
- 不修改数据库字段，不修改前端布局样式。

## 实现

- 新增 `D:\Projects\dingchang_frontend_full\src\utils\errorMessage.js`，统一解析接口错误。
- `src/api/http.js` 响应拦截器会先解析 Blob 错误 JSON，再标准化 AxiosError：
  - `error.apiErrorDetail`：真实原因。
  - `error.apiErrorMessage`：带业务动作/HTTP 状态/接口信息的中文提示。
  - `error.response.data.detail`：若原始 detail 是数组或英文，会转成中文原因，旧页面写法也能受益。
- 重点页面接入 `getApiErrorMessage()`：
  - 登录、账号列表/创建/编辑/删除。
  - 客户、渠道列表/新增/编辑/删除。
  - 订单创建、订单导入、订单详情、订单/财务列表、财务状态写入、财务导出。
  - AI 助手会话、上传、绑定订单材料。
  - 客户/渠道远程下拉错误状态。

## 解析规则

- FastAPI `HTTPException(detail="...")`：直接展示 detail；若为常见英文原因则翻译。
- Pydantic 422 数组：转换为 `字段：原因`，例如 `用户名：长度不能少于 1 个字符；密码：长度不能少于 1 个字符`。
- 常见英文转换：
  - `No permission` -> `权限不足：当前账号没有执行该操作的权限`
  - `Order not found` -> `订单不存在或已被删除`
  - `Missing X-Session-Token` -> `未登录或登录信息已失效：缺少会话令牌`
  - `Only finished orders can be accessed in finance` -> `仅已完成订单可进入财务详情`
- 网络错误：
  - 超时：提示服务器未在限定时间响应。
  - Network Error / Failed to fetch：提示网络、代理或本地服务启动状态。
- 文件下载错误：
  - 先尝试把 Blob JSON 错误解开，再按同一套规则提示真实原因。

## 自检

- 静态扫描：未发现仍直接使用后端 `detail` 数组或泛化 `操作失败/下载失败/无权限` 的接口错误弹窗。
- 前端构建：`npm run build` 通过。
- 后端错误样本：
  - 无 token 访问 `/api/users` 返回 401 `Missing X-Session-Token`，前端会提示缺少会话令牌。
  - 错误密码登录返回 401 `用户名或密码错误`，前端会原样提示。
  - 空用户名/密码登录返回 422 数组，前端会转成字段级中文原因。

## 剩余边界

- 若后端发生未捕获 500，前端只能展示“服务器内部错误 + 接口信息”；真正堆栈仍应以服务端日志为准，避免把敏感内部信息暴露给普通用户。
- 本轮只治理错误弹窗与错误状态，不调整页面布局样式。

