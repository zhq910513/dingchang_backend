# 图片上传与鉴权全链路审查（2026-05-03）

## 本地数据抽样

抽样时间：2026-05-03。

| 项目 | 结果 |
| --- | ---: |
| `image_file_new` | 10617 |
| `order_image_new` | 11062 |
| 有图片订单 | 3057 |
| slot 分布 | `related=3080`、`idcard_front=2609`、`driving_license_main=1950`、`idcard_back=1567`、`driving_license_sub=1015`、`vehicle_cert=841` |

样本发现：历史 `order_image_new.image_url` / `image_file_new.url` 保存的是 `https://dingchang.fwh.bcebos.com/...` 公开直链。匿名真实 GET `idcard/22/0f/220f8b4c0de0ec88dcf45539af9d46e2.jpg` 返回 `200 image/jpeg`，说明云桶或 CDN 当前仍允许公开读取。

## 已修复

1. 后端展示 URL 不再强制公开直链：`app/utils/order_image_urls.py` 改为通过 `StorageService.object_url_for_display(signed=None)` 生成展示 URL，并在启用签名时禁止降级公开 URL。
2. 默认与本地配置启用签名 GET：`BOS_SIGNED_GET_URL=True`，本地 `.env` 已改为 `BOS_SIGNED_GET_URL=1`。
3. 图片绑定不再信任前端传入 URL：`orders.finalize` 与 `/orders/{id}/images/bind` 对 storage-backed 图片只保存 `storage_key` / 元数据，历史 URL 仅作为无法生成存储展示 URL 时的兼容回退。
4. STS 权限收窄：`app/services/bce_sts.py` 仅允许 `cert/*`、`idcard/*`、`dl/*`、`backup/*` 的 `READ/WRITE`，移除 bucket `LIST` 和全桶通配权限。
5. 前端直传不再走 public HEAD，也不再构造 public preview URL：`src/utils/bosUpload.js` 改为 signed HEAD、signed preview URL，并修正 BCE 签名算法与后端 `bce_auth.py` 一致。
6. 前端保存图片元数据时不再把预览 URL 回传给后端：`src/api/orders.js` 的 `sanitizeImages` 仅提交 `slot_key/storage_key/md5/etag/size/content_type/original_name`。
7. 全局取消公开直链降级：`StorageService.object_url_for_display` 默认不再 `allow_fallback_public`；订单详情、代理上传、AI 助手、OCR worker 都要求签名链路成功，不再静默回退 BOS 公网 URL。

## 真实测试

测试日志：

| 日志 | 结论 |
| --- | --- |
| `logs/image-auth-real-test-20260503140313.json` | 38 步，0 失败 |
| `logs/frontend-bos-upload-real-20260503060956.json` | 真实执行前端 `src/utils/bosUpload.js` 模块，4 步，0 失败 |
| `logs/image-auth-retest-20260503150514.json` | 最新代码重启后复测，36 步，0 失败；复测脚本为 `scripts/image_auth_retest.py` |

覆盖项：

| 场景 | 结果 |
| --- | --- |
| 匿名访问 `/orders/bos-sts`、订单详情 | 401 |
| `market` 获取 STS、代理上传、绑定图片 | 403 |
| `finance` 上传 `vehicle_cert` | 403 |
| `finance` 上传 `related` | 200 |
| `finance` 未完成订单绑定 `related` | 400 |
| `finance` 已完成订单绑定 `related` | 200 |
| storage_key slot/md5 不匹配 | 400 |
| 订单详情返回图片 URL | 含 `authorization` 与 `x-bce-security-token` 的签名 URL |
| 匿名访问签名 URL | 200，短期授权可读 |
| STS 直传允许前缀 `backup/*` | 200 |
| STS 直传非法前缀 `not_allowed/*` | 403 |
| STS bucket LIST | 403 |

最新复测额外确认：前端/调用方即使提交恶意 `url=https://evil.invalid/...`，后端在启用对象存储时也不会写入 `order_image_new.image_url` 或 `image_file_new.url`；订单详情返回的仍是后端生成的短期签名 URL。

本轮本地 DB 测试数据已清理：测试用户、测试订单、`order_image_new`、本轮 DB 侧 `image_file_new` 残留均为 0。云端真实上传对象无法通过当前项目安全删除接口清理，已记录在测试日志中。

## 残余风险

云桶/CDN 当前仍可匿名读取已知公开 URL。代码已经不再主动泄露公开直链，并且 STS 已限制前缀与禁止 LIST；但如果外部已经知道某个历史 `storage_key`，云侧仍可能允许直接访问。要做到“知道 key 也打不开”，需要在 BOS/CDN 控制台或云侧 API 将 bucket/CDN 改为私有，并确认所有图片展示都走签名 URL 后再执行。该动作影响生产访问面，本轮未贸然修改云桶 ACL。
