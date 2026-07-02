# twin-gitcall — парсер-секвенсор 1С для Corezoid GIT Call

Код, который **Corezoid GIT Call** (узел `api_git`, `lang: python`) тянет из этого репозитория и
исполняет. Узел **только парсит** артефакт 1С и возвращает следующий пакет задач + курсор —
**ничего не создаёт в Simulator**. Сборку (формы/акторы/счета/двойная запись/связи) делает
Corezoid-процесс `twin-loop`, вызывая Simulator REST в своих api-узлах и храня состояние ref→id.

## Контракт узла

GIT Call вызывает `usercode(data, context)` из `usercode.py`. Один вызов = одно окно
(**≤ ~1.4 МБ сериализовано ИЛИ ≤ ~25 с**, что раньше) → укладывается в лимит 30 с GIT Call.

**Вход (`data`):**
- `source_url` — ссылка http(s) на файл (или путь, доступный раннеру)
- `scope` — `full` (структура+данные) | `structure` (только формы). По умолчанию `full`.
- `cursor` — непрозрачный токен из предыдущего вызова; отсутствует/null на первом вызове.

**Выход (добавляется в `data`):**
- `tasks` — пакет типизированных задач в порядке зависимостей
  (`create_form → create_actor → fill_actor → transfer → create_link`)
- `cursor` — передать обратно на следующем вызове
- `done` — `true` ⇒ задач больше нет, цикл завершается
- `format`, `count` — формат артефакта и размер пакета
- `twin_error` — только при ошибке

Поддерживаемые форматы: бинарные `.1CD/.dt/.cf/.cfe` (через `onec_dtools`), EnterpriseData-XML,
структурные контейнеры. Для `.1CD` — настоящий оконный курсор по стадиям; для XML/контейнеров
небольшой набор задач отдаётся одним окном.

## Файлы

| Файл | Назначение |
|---|---|
| `usercode.py` | Точка входа GIT Call (stateless оконный парсер). |
| `extractor.py` | Разбор `.1CD` по стадиям оконным курсором (`window()` / `STAGES`). |
| `analyze.py` | Детект формата + инвентаризация сущностей/метрик. |
| `build_twin.py` | Доноры: `op_stream_xml`, `fetch_to_temp` (стрим-загрузка по URL). |
| `requirements.txt` | `onec_dtools` (ставится раннером GIT Call при pull репо). |

## Локальная проверка (parity)

```bash
python3 usercode.py <file|url> [full|structure]
# гоняет полный цикл по курсору, печатает: format, steps, max_batch_bytes, totals, first_ops
```

`max_batch_bytes` должен быть ≤ 1.4 МБ; `first_ops` начинается с `create_form` (порядок зависимостей).

## Привязка к Corezoid

1. Опубликовать этот репозиторий (см. `PUBLISH.md`).
2. Прописать его URL в env var `@twin-git-repo` (folder/stage `681528`).
3. Узел `api_git` процесса `twin-loop`: `repo={{env_var[@twin-git-repo]}}`, `commit=main`,
   `path=usercode.py`, `lang=python`.
</content>
