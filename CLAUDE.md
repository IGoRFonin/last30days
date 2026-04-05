# last30days Skill

Fork of [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) with direct Reddit API via proxy pool.
Python scripts with multi-source search aggregation.

## Structure
- `scripts/last30days.py` — main research engine
- `scripts/lib/` — search, enrichment, rendering modules
- `scripts/lib/proxy_pool.py` — round-robin proxy pool with cooldown and state persistence
- `scripts/lib/reddit_direct.py` — Reddit JSON API через прокси (замена ScrapeCreators)
- `scripts/lib/vendor/bird-search/` — vendored X search client
- `SKILL.md` — skill definition (deployed to ~/.claude/skills/last30days/)

## Commands
```bash
python3 scripts/last30days.py "test query" --emit=compact  # Run research
bash scripts/sync.sh                                        # Deploy to ~/.claude, ~/.agents, ~/.codex
```

## Rules
- `lib/__init__.py` must be bare package marker (comment only, NO eager imports)
- After edits: run `bash scripts/sync.sh` to deploy

## Git Remotes & Sync with Upstream

```
origin   = git@github.com:IGoRFonin/last30days.git   (наш форк, сюда пушим)
upstream = https://github.com/mvanhorn/last30days-skill.git (оригинал mvanhorn, read-only)
```

### Ключевые отличия от upstream
- **Reddit через прокси** (`reddit_direct.py` + `proxy_pool.py`) вместо ScrapeCreators API
- Конфиг `REDDIT_PROXIES_FILE` вместо `SCRAPECREATORS_API_KEY` для Reddit
- ScrapeCreators остался только для TikTok/Instagram
- `reddit.py` (ScrapeCreators Reddit) удалён
- **X/Twitter через фиксированный прокси** — `BIRD_PROXY` в `.env` для проксирования всех запросов Bird search

### Как обновиться с upstream
```bash
git fetch upstream
git merge upstream/main
```
Возможные конфликты при merge:
- `scripts/last30days.py` — функция `_search_reddit()` переписана, при конфликте сохранять нашу версию
- `scripts/lib/env.py` — добавлены `REDDIT_PROXIES_FILE` и `BIRD_PROXY`, обновлены `is_reddit_available`/`get_reddit_source`
- `scripts/lib/bird_x.py` — добавлен `set_proxy()` и передача `BIRD_PROXY` в Node subprocess
- `scripts/lib/vendor/bird-search/bird-search.mjs` — добавлена инициализация `undici.ProxyAgent` по `BIRD_PROXY`
- `scripts/lib/reddit.py` — удалён в нашем форке, upstream может обновлять его; при конфликте удалять файл
- `README.md`, `SKILL.md` — полностью наши версии, при конфликте сохранять наши
