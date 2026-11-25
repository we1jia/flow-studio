# 第三方声明 / Third-Party Notices

## gflow-cli

`flow_studio.py` 中生图直连部分的请求线格式与回包解析移植自 gflow-cli（提交 `6eadc3d`，v0.106.0）：

- `src/gflow_cli/api/transports/frame_builder.py`（`build_ogiz0b_body`）
- `src/gflow_cli/api/transports/batchexecute.py`（`_wrb_rows` / `parse_frames` / `RpcError`）
- `src/gflow_cli/api/transports/direct_http.py`（模型线名、reCAPTCHA action、可回退的状态码）

Source: https://github.com/ffroliva/gflow-cli

```
MIT License

Copyright (c) 2026 Flavio Oliva (ffroliva)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
