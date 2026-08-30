<p align="center"><img src="assets/cover.png" alt="Netease to Spotify" /></p>

# Netease to Spotify

将网易云音乐歌单、关注歌手和收藏专辑迁移到 Spotify。这个分支在原项目基础上补充了 Spotify 2026 Development Mode API 兼容、歌单断点续传、批量写入、未匹配报告，以及网易云“关注歌手/收藏专辑”迁移。

> 注意：歌曲匹配依赖两个平台的搜索结果，不能保证 100% 准确。请在迁移后核对歌单顺序和未匹配报告。Spotify 可能限制 Development Mode 应用的用户、搜索和调用配额，大型曲库迁移可能需要分次进行。

## 功能

- `cli.py`：迁移一个或多个网易云歌单。
- `netease_playlist_resume_2026.py`：按源歌单顺序分批迁移，支持断点续传和复用已有 Spotify 歌单。
- `netease_library_to_spotify_2026.py`：迁移网易云关注歌手和收藏专辑，需要本人的网易云网页 Cookie。
- `clear_spotify_saved_albums_2026.py`：可选清理工具。它会先导出 CSV 备份，再要求输入完整确认语句后才取消收藏全部 Spotify 专辑。

## 安装

1. 需要 Python 3.9 或更高版本。
2. 安装依赖：

   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. 在 [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) 创建应用，并将 Redirect URI 设为与配置完全一致的回调地址，例如 `http://127.0.0.1:8888/callback`。使用Spotify Developer应用需要你拥有Spotify Premium订阅。
4. 复制示例配置：

   ```bash
   cp config.example.yml config.yml
   ```

5. 填写 `config.yml`。

## 用法

迁移 `config.yml` 中的单个歌单：

```bash
python3 cli.py
```

一次迁移多个歌单：

```bash
python3 cli.py -l <歌单ID1> <歌单ID2>
```

使用断点续传版：

```bash
python3 netease_playlist_resume_2026.py
python3 netease_playlist_resume_2026.py --resume-index 694 --playlist-id "<Spotify 歌单 ID 或链接>"
```

迁移关注歌手和收藏专辑：

```bash
python3 netease_library_to_spotify_2026.py
python3 netease_library_to_spotify_2026.py --artists
python3 netease_library_to_spotify_2026.py --albums
```

这项功能需要登录后的网易云 Cookie。可将它保存在本地 `config.yml` 的 `netease_cookie` 字段，或临时通过 `NETEASE_COOKIE` 环境变量传入。

可选清空 Spotify 收藏专辑：

```bash
python3 clear_spotify_saved_albums_2026.py
```

该脚本会修改 Spotify 账号数据。请先检查生成的 CSV 备份，并确认你确实希望取消收藏全部专辑。

## 本地生成文件与隐私

脚本会在本地生成 OAuth 令牌缓存、断点文件、未匹配 CSV 和收藏专辑备份。这些文件可能包含账号凭据、歌单 ID、音乐偏好或其他个人数据。

## 原项目与许可

本项目是 [muyangye/Netease_To_Spotify](https://github.com/muyangye/Netease_To_Spotify) 的衍生版本。感谢原作者 [Muyang Ye](https://github.com/muyangye) 及原项目贡献者。原项目和本衍生版本均依照 [MIT License](LICENSE) 发布；原始版权声明和许可条款已保留。更详细的来源与改动说明见 [NOTICE.md](NOTICE.md)。

这是社区项目，与网易云音乐或 Spotify 没有官方关联。请自行遵守两个平台的条款及所在地法律。

## 上游项目鸣谢

- [pyncm](https://github.com/mos9527/pyncm)
- [Spotipy](https://github.com/spotipy-dev/spotipy)
- [Binaryify/NeteaseCloudMusicApi issue #1121](https://github.com/Binaryify/NeteaseCloudMusicApi/issues/1121#issuecomment-774438040)
