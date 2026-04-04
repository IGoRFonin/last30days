# Reddit Direct: замена ScrapeCreators на прямые запросы с прокси

**Дата:** 2026-04-04
**Статус:** Утверждён

## Мотивация

Заменить платный ScrapeCreators API для Reddit на прямые запросы к публичному Reddit JSON API с ротацией прокси. Цель — экономия.

## Архитектура

### Новый модуль `scripts/lib/reddit_direct.py`

Заменяет `scripts/lib/reddit.py` (ScrapeCreators). Drop-in замена с тем же интерфейсом `search_and_enrich()`.

**Компоненты:**

```
reddit_direct.py
├── ProxyPool            — round-robin ротация, cooldown на 429, state persistence
├── _fetch_json()        — запрос через прокси с retry (макс 5 попыток)
├── _global_search()     — reddit.com/search.json
├── _subreddit_search()  — reddit.com/r/{sub}/search.json
├── fetch_post_comments()— reddit.com/comments/{id}.json
├── search_reddit()      — оркестрация (query expansion → global → subreddit drill-down → dedup → filter)
├── enrich_with_comments()— обогащение комментариями
└── search_and_enrich()  — search + comments, drop-in замена reddit.search_and_enrich
```

### ProxyPool

```python
class ProxyPool:
    def __init__(self, proxies: List[str], state_file: Path):
        self._proxies = proxies          # список URL прокси
        self._index = загрузить из state_file или 0
        self._cooldowns = {}             # {proxy: timestamp_когда_можно}
        self._lock = threading.Lock()    # потокобезопасность

    def next(self) -> Optional[str]:
        """Round-robin, пропуская остывающие прокси."""
        # Обходит список максимум один круг
        # Если все на cooldown — ждёт минимальный оставшийся cooldown

    def cooldown(self, proxy: str, seconds: float = 30):
        """Пометить прокси как остывающий после 429."""

    def save_state(self):
        """Сохранить текущий индекс в state_file."""
```

**Поведение:**
- Round-robin по списку 50 прокси
- 429 → cooldown 30 сек для этого прокси
- Connection error / timeout → следующий прокси (без cooldown)
- 403 / HTML anti-bot → следующий прокси
- Если все на cooldown → ждать минимальный оставшийся
- State persistence: `~/.config/last30days/proxies.state` — одно число (индекс), создаётся/обновляется автоматически

### Reddit JSON API эндпоинты

| Функция | URL | Парсинг |
|---------|-----|---------|
| Global search | `reddit.com/search.json?q={q}&sort=relevance&t=month&limit=25` | `data.children[].data` |
| Subreddit search | `reddit.com/r/{sub}/search.json?q={q}&restrict_sr=on&sort=relevance&t=month` | `data.children[].data` |
| Comments | `reddit.com/comments/{post_id}.json` | `[1].data.children[].data` |

### Retry-логика (на каждый запрос)

- Макс 5 попыток с разными прокси
- User-Agent ротация из списка ~10 браузерных строк
- Timeout 15 сек
- 5 прокси подряд не сработали → пустой результат

### Конфигурация

**В `~/.config/last30days/.env`:**
```
REDDIT_PROXIES_FILE=~/.config/last30days/proxies.txt
```

**`proxies.txt`** — по одному прокси на строку:
```
http://user:pass@host1:port
http://user:pass@host2:port
...
```

**`proxies.state`** — автосоздаваемый, одно число:
```
37
```

## Иерархия fallback в `_search_reddit()`

```
1. reddit_direct  (если REDDIT_PROXIES_FILE настроен)
2. reddit_public  (без прокси, как сейчас)
3. OpenAI         (legacy)
```

## Изменения в файлах

| Файл | Действие |
|------|----------|
| `scripts/lib/reddit_direct.py` | Создать — основной модуль |
| `scripts/lib/reddit.py` | Удалить — ScrapeCreators больше не нужен |
| `scripts/lib/env.py` | Добавить `REDDIT_PROXIES_FILE`, убрать Reddit-зависимость от `SCRAPECREATORS_API_KEY` |
| `scripts/last30days.py` | Заменить `reddit.search_and_enrich` → `reddit_direct.search_and_enrich` |
| Тесты | Обновить/создать тесты для reddit_direct |

## Переиспользование

- Query expansion, subreddit discovery, normalization, relevance scoring — переносятся из `reddit.py` (логика не зависела от ScrapeCreators)
- `_parse_posts()` из `reddit_public.py` — парсинг Reddit JSON формата
- DEPTH_CONFIG — тот же формат глубины

## Что НЕ меняется

- `reddit_public.py` — остаётся как fallback без прокси
- Интерфейс `search_and_enrich()` — тот же (topic, from_date, to_date, depth, token→proxies)
- Формат выходных данных — тот же (items с title, url, subreddit, engagement, top_comments и т.д.)
