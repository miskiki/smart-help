# models/ — 模型资产

模型权重大、且不便走 Git；仓库只保留**嵌入模型分词器**（约 11MB 的公开文本资产），
其余 GGUF 权重与 `llama-server` 由同事另行准备。私密数据包内也放了一份 tokenizer，
用于离线或校验场景，二者择一即可。

## 仓库内包含

| 路径 | 说明 |
|---|---|
| `models/README.md` | 本说明 |
| `models/qwen3-embedding-4b/tokenizer.json` | Qwen3-Embedding-4B 分词器（精确 token 预算/截断） |
| `models/qwen3-embedding-4b/sentence_bert_config.json` | 嵌入模型配置（26 字节） |

## 运行时需要的文件

| 文件 | 大小 | 用途 | 来源 |
|---|---|---|---|
| `models/qwen3-embedding-4b/tokenizer.json` | 11MB | 嵌入模型分词器（`EMBED_TOKENIZER_PATH`） | ✅ 仓库内置（私密包也有一份） |
| `models/qwen3-embedding-4b/sentence_bert_config.json` | 26B | 嵌入模型配置 | ✅ 仓库内置 |

## 不在仓库、也不在私密包内的 GGUF 大模型

以下文件体积达数十 GB，需同事自行准备（从原维护机拷贝或重新下载），并在
`deploy/web.env` 中配置路径：

| 变量 | 示例文件 | 大小 |
|---|---|---|
| `LOCAL_LLM_SERVER` | `llama.cpp` 编译产物 `build/bin/llama-server` | ~92MB |
| `LOCAL_LLM_MODEL` | `Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf` | ~22GB |
| `LOCAL_EMBED_GGUF` | `Qwen3-Embedding-4B-Q4_K_M.gguf` | ~2.4GB |
| `LOCAL_RERANK_GGUF` | `Qwen3-Reranker-4B.Q4_K_M.gguf` | ~2.6GB |

`deploy/start.sh` 启动前会检查 `LOCAL_LLM_SERVER`、`LOCAL_LLM_MODEL`、`LOCAL_EMBED_GGUF`、
`LOCAL_RERANK_GGUF` 是否存在，缺失会直接失败并打印缺哪个文件。

## 参考

- 部署步骤：`docs/部署与启动指南.md`
- 数据契约：`docs/数据准备与目录说明.md`
