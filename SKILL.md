---
name: skill-consolidator
description: ばらけたスキルを一か所（C:\Users\0501JP\.config\opencode\skills、単一ソース）へ集約し、重複・壊れリンク・フロントマターを是正する。「スキルをまとめる」「スキル集約」「skillまとめ」「consolidate」「重複解消」「スキル統合」「スキルを一本化」などで発動。skill-crawler で棚卸しした結果をもとに、symlink の実体コピー、同名SKILL.mdの同一判定、name/dir 不一致や description 欠落の修正、WSL残骸の削除まで行う。
---

# skill-consolidator — スキル集約・一本化

バラバラの場所（WSL / ~/.agents / ~/.claude / repos / symlink）にあるスキルを、
**単一ソース `C:\Users\0501JP\.config\opencode\skills`** へ集約する。

## 前提

- 集約先（ターゲット）: `C:\Users\0501JP\.config\opencode\skills`
- WSL の global は `/mnt/c/Users/0501JP/.config/opencode/skills` への symlink → 実質同じ
- まず `skill-crawler` を実行して現状（重複 / BROKEN / SKILL.md:NO / 名不一致）を把握する
- スキル名は全場所で一意にする（loader が混乱するため）

## 集約手順

1. **実体を取得**: ソースが symlink なら `readlink`/`Target` で実体パスを取り、リンク先の実体をコピー（リンクごとコピーしない）
2. **壊れ（BROKEN）**: 実体が無いものはコピー不能 → 一覧に「SKIP」として明示し、エージェントは**削除を提案**する（勝手に削除しない。確認を取る）
3. **重複（同名）**:
   - `SKILL.md` の内容をハッシュ比較（`Get-FileHash` / `sha256sum`）
   - 同一 → スキップ＋元フォルダ削除を提案
   - 差異あり → どちらを採用するか**人間に確認**（推奨: 新しい方 or シンプル派）→ 採用しない側は削除提案
4. **コピー実行**: ターゲットに無いスキルだけ `Copy-Item -Recurse`（`-Force` で同一上書き ok）
5. **フロントマター是正**: コピー後、先行している `SKILL.md` の frontmatter を検査
   - `name:` がフォルダ名と一致するか（一致しないとロードされない）
   - `description:` が 1-1024 字で存在するか（無いとモデルに表示されない）
   - `name` は `^[a-z0-9]+(-[a-z0-9]+)*$` に従うこと
6. **原本の整理**: 一本化の基本方針により、コピー成功を確認したら WSL / ~/.agents / ~/.claude 側の原本を**削除してよい**（確認を取る）。壊れ symlink・空フォルダは即削除候補
7. **最終検証**: ターゲットを再走査して「SKILL.md:YES 全員・重複ゼロ・BROKENゼロ」を確認

## コマンド例（PowerShell）

```powershell
$dst = 'C:\Users\0501JP\.config\opencode\skills'
# 実体変換（symlink なら Target へ）＋コピー
Get-ChildItem -LiteralPath 'C:\Users\0501JP\.agents\skills' -Force |
  ForEach-Object {
    $src = if ($_.LinkType -eq 'SymbolicLink') { $_.Target } else { $_.FullName }
    if (-not (Test-Path -LiteralPath (Join-Path $src 'SKILL.md'))) {
      Write-Output "SKIP(broken/no-skill): $($_.Name)"; return
    }
    $t = Join-Path $dst $_.Name
    if (-not (Test-Path -LiteralPath $t)) { Copy-Item -LiteralPath $src -Destination $t -Recurse }
    else {
      $h1 = (Get-FileHash -LiteralPath (Join-Path $src 'SKILL.md')).Hash
      $h2 = (Get-FileHash -LiteralPath (Join-Path $t  'SKILL.md')).Hash
      Write-Output (if ($h1 -eq $h2) { "SAME: $($_.Name)" } else { "DIFF: $($_.Name) → 確認要求" })
    }
  }
```

WSL(bash) の場合:
```bash
dst=/mnt/c/Users/0501JP/.config/opencode/skills
for s in ~/.agents/skills/* ~/.claude/skills/*; do
  [ -e "$s" ] || { echo "BROKEN: $s"; continue; }
  n="$(basename "$s")"
  [ -f "$s/SKILL.md" ] || { echo "SKIP(no skill): $n"; continue; }
  [ -d "$dst/$n" ] || cp -r "$s" "$dst/$n"
  a=$(sha256sum "$s/SKILL.md" | cut -d' ' -f1)
  if [ -f "$dst/$n/SKILL.md" ]; then
    b=$(sha256sum "$dst/$n/SKILL.md" | cut -d' ' -f1)
    [ "$a" = "$b" ] && echo "SAME: $n" || echo "DIFF: $n → 確認要求"
  fi
done
```

## 是正ルール（SKILL.md frontmatter）

- フォルダ名と `name:` 不一致 → `name:` をフォルダ名に書き換え（フォルダを変えない）
- `description:` 欠落 → 1文（日本語可）で何/いつ使うかを追記
- 不要な `metadata:` 等は温存可

## このスキルの自己管理

- 自分 / skill-crawler は ターゲット（単一ソース）に共存する。互いを消さない
- 実行後は必ず `skill-crawler` で再走査し、残骸ゼロを確認する
- 保留した削除（人間確認待ち）は一覧として返す