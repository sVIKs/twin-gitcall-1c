# Публикация репо для GIT Call (последний шаг)

GIT Call раннер тянет код по HTTPS/SSH. Нужен публично-читаемый (или доступный раннеру) git-репо.
Вы создаёте репозиторий; ниже — команды залить туда содержимое `twin-gitcall-repo/`.

## 1. Создайте пустой репозиторий
На GitHub/GitLab (например `twin-gitcall`). Скопируйте его URL, напр.
`https://github.com/<you>/twin-gitcall.git`.

> Раннеру нужен доступ на чтение. Для приватного репо настройте deploy-key/токен на стороне
> Corezoid; для публичного — ничего. В коде секретов нет (токен Simulator приходит из env var
> процесса, не из репо).

## 2. Залейте содержимое
Из каталога проекта:

```bash
cd twin-gitcall-repo
git init
git add usercode.py extractor.py analyze.py build_twin.py requirements.txt README.md
git commit -m "twin gitcall parser-sequencer"
git branch -M main
git remote add origin <ВАШ_URL>.git
git push -u origin main
```

## 3. Дайте мне URL
Скажите URL — я:
1. создам env var `@twin-git-repo` = `<ВАШ_URL>.git` в folder/stage `681528`;
2. узел `api_git` в `twin-loop` уже ссылается на `{{env_var[@twin-git-repo]}}`, `commit=main`,
   `path=usercode.py`, `lang=python`;
3. прогоню `run-task` на демо-файле и проверю, что GIT Call отдаёт пакеты, а цикл строит двойника.

## Обновление кода потом
```bash
cd twin-gitcall-repo
git add -A && git commit -m "update" && git push
```
GIT Call берёт `commit=main` — новый код подхватится со следующего вызова (для прод-стабильности
можно зафиксировать тег/commit-hash вместо `main`).
