# Запуск DonorFlow через Docker

## Требования

Установите Docker Desktop и убедитесь, что Docker Engine запущен:

```powershell
docker --version
docker compose version
```

## Первый запуск

Из корня проекта выполните:

```powershell
docker compose up --build
```

После запуска:

- frontend: http://localhost:5173
- backend API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- health-check: http://localhost:8000/health

Вход по умолчанию:

```text
Логин: coordinator
Пароль: coordinator123
```

Контейнер backend при старте автоматически выполняет `seed.py`. Скрипт идемпотентен и не удаляет существующие записи.

## Запуск в фоне

```powershell
docker compose up --build -d
```

Проверить состояние:

```powershell
docker compose ps
```

Посмотреть логи:

```powershell
docker compose logs -f backend
```

Остановить контейнеры, сохранив базу:

```powershell
docker compose down
```

Остановить контейнеры и удалить данные PostgreSQL:

```powershell
docker compose down -v
```

## Смена credentials

Перед запуском можно задать переменные окружения в PowerShell:

```powershell
$env:JWT_SECRET_KEY="строка-длиной-минимум-32-символа"
$env:COORDINATOR_USERNAME="admin"
$env:COORDINATOR_PASSWORD="сложный-пароль"
docker compose up --build -d
```

Или создать `.env` в корне проекта:

```dotenv
JWT_SECRET_KEY=строка-длиной-минимум-32-символа
COORDINATOR_USERNAME=admin
COORDINATOR_PASSWORD=сложный-пароль
```

Файл `.env` не нужно добавлять в git.

## Архитектура

- `db`: PostgreSQL 16, данные в volume `postgres_data`.
- `backend`: FastAPI на порту 8000 внутри и снаружи Docker.
- `frontend`: production-сборка React/Vite в Nginx на порту 5173.
- Nginx проксирует `/api/*` и `/health` в контейнер `backend`, поэтому браузер использует единый origin `http://localhost:5173`.
