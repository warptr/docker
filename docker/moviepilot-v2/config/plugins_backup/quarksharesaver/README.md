# QuarkShareSaver

轻量夸克分享转存插件。

它只负责一件事：

- 把夸克分享链接直接转存到你自己的夸克网盘目录

适合的调用方式：

- 智能体调用插件 API
- 飞书桥接发送简短命令

推荐接口：

- `GET /api/v1/plugin/QuarkShareSaver/health`
- `GET /api/v1/plugin/QuarkShareSaver/folders?path=/`
- `POST /api/v1/plugin/QuarkShareSaver/share/info`
- `POST /api/v1/plugin/QuarkShareSaver/transfer`

`transfer` 请求体示例：

```json
{
  "url": "https://pan.quark.cn/s/xxxxxxxx",
  "access_code": "abcd",
  "path": "/来自分享/夸克"
}
```

飞书推荐命令：

```text
夸克转存 https://pan.quark.cn/s/xxxxxxxx pwd=abcd path=/最新动画
```

配置重点：

- `Cookie` 使用浏览器登录 `pan.quark.cn` 后复制完整 Cookie
- `默认保存目录` 建议填一个固定路径，例如 `/来自分享/夸克`

这类轻插件更适合做“稳定执行层”：

- 智能体负责理解意图和补参数
- 插件负责真正转存
