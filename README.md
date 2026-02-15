# agent-service

Python сервис для работы с OpenWebUI через HTTP-запросы (browser-like), с локальным хранением сессии в проекте и desktop UI для агентной разработки.

## Быстрый старт (через virtual env)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Подготовка конфига:

```bash
mkdir -p .agent-service
cp config.example.yaml .agent-service/config.yaml
```

Запуск desktop-окна (рекомендуется):

```bash
make desktop
```

Или вручную:

```bash
PYTHONPATH=src .venv/bin/python -m agent_service.desktop
```

Режим отладки c изолированным workspace `./test`:

```bash
PYTHONPATH=src .venv/bin/python -m agent_service.desktop --test
```

Запуск API-сервиса (если нужен HTTP API):

```bash
source .venv/bin/activate
uvicorn agent_service.main:app --app-dir src --host 0.0.0.0 --port 8080
```

Или через `Makefile`:

```bash
make install
make run
```

## Эндпоинты MVP

1. `POST /auth/login`
2. `GET /auth/status`
3. `GET /models`
4. `GET /chats`
5. `POST /chats`
6. `DELETE /chats/{chat_id}`
7. `POST /messages`
8. `POST /agent/tasks`
9. `GET /health`

`POST /agent/tasks` запускает агентный цикл с локальными инструментами:
1. `list_files`
2. `read_file`
3. `search_in_files`
4. `write_file`
5. `replace_in_file`
6. `delete_file`

## UI

По умолчанию используйте desktop UI (без браузера): `make desktop`.

Интерфейс:

1. Стартовая ширина окна `600px`, высота занимает экран, окно можно расширять.
2. Два варианта темы: `black-white` и `white-black`.
3. Вверху поле URL для OpenWebUI и кнопка `Connect`.
4. Если сессия требует логин, отображается блок авторизации (username/password).
5. Если авторизация не нужна, сверху отображается выбор модели и создание чата.
6. По центру область результата/ответов агента.
7. Внизу поле ввода сообщения и кнопка отправки.
8. Кнопка `...` в заголовке скрывает/показывает верхние блоки подключения и выбора модели.
9. В блоке модели есть список существующих чатов, можно выбрать чат и продолжить в нем.
10. Отправка из desktop использует agent-loop: модель может читать и изменять файлы текущего проекта.
11. Изменения файлов идут через preview: появляется отдельный блок `Изменения` со списком файлов, подсветкой diff и кнопками `Принять все`/`Отменить`.
12. Корень workspace для desktop:
   - `--test` (или positional `test`) -> `./test`
   - без флага -> `agent.project_path` из конфига, если задан
   - без флага и без `agent.project_path` -> каталог уровнем выше текущего (`../`)

## Локальное хранение

Сервис хранит данные только в `./.agent-service`:

1. `config.yaml`
2. `auth.json`
3. `cookies.json`
4. `chats.json`
5. `chat_context.db`
6. `service.log`

`chat_context.db` хранит локальную историю сообщений по `chat_id` и используется как fallback-контекст, если OpenWebUI не отдает историю чата.

Для выбора целевого проекта можно указать путь в конфиге:

```yaml
agent:
  project_path: "/absolute/or/relative/path/to/project"
```

Относительный путь считается от каталога, из которого запускается desktop.

## Troubleshooting

1. Если видите `404 Not Found` на `auths`, сервис теперь пробует несколько вариантов auth endpoint автоматически.
2. При необходимости задайте endpoint вручную в `./.agent-service/config.yaml`:
   - `openwebui.endpoints.session_check`
   - `openwebui.endpoints.signin`
3. Если `session_check` возвращает `token/token_type`, сервис автоматически сохраняет их и использует `Authorization: Bearer ...` в следующих запросах.

## Примеры запросов

```bash
curl -s http://127.0.0.1:8080/health

curl -s -X POST http://127.0.0.1:8080/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"user","password":"pass"}'

curl -s http://127.0.0.1:8080/models

curl -s http://127.0.0.1:8080/chats

curl -s -X POST http://127.0.0.1:8080/chats \
  -H 'content-type: application/json' \
  -d '{"model_id":"your-model-id","title":"Agent bootstrap"}'

curl -s -X DELETE http://127.0.0.1:8080/chats/<chat_id>

curl -s -X POST http://127.0.0.1:8080/messages \
  -H 'content-type: application/json' \
  -d '{"model_id":"your-model-id","message":"Hello"}'

curl -s -X POST http://127.0.0.1:8080/agent/tasks \
  -H 'content-type: application/json' \
  -d '{
    "model_id":"your-model-id",
    "chat_id":"optional-chat-id",
    "message":"Прочитай README.md и обнови раздел API, если не хватает /agent/tasks",
    "auto_apply": true
  }'
```
