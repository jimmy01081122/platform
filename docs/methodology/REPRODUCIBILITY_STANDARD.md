# Reproducibility Standard

## 必須保存

- Git commit、dirty diff hash。
- experiment spec 與 resolved config。
- tool name/version/build command/container digest。
- input dataset ID、checksum、conversion revision。
- platform profile revision。
- random seeds。
- exact command and return code。
- stdout/stderr。
- normalized metrics and units。
- artifacts index。
- failure classification。

## 參數來源分類

每個性能敏感參數必須標記：

- `measured`
- `derived`
- `vendor_spec`
- `tool_default`
- `assumed`
- `swept`

`assumed` 參數不得單點使用；必須做 sensitivity sweep。

## 可重複等級

- R0：命令與輸入已記錄。
- R1：同一環境可重跑。
- R2：乾淨 container/VM 可重跑。
- R3：不同 host 取得統計一致結果。
- R4：替代工具或高保真模型支持同一工程結論。

不是每個探索都必須立刻達到 R4，但主線 closure 應至少達到 R2。
