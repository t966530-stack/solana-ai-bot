SOLANA AI BOT — backend для Render

Это серверная часть прототипа. Она отдаёт health-check и публичные рыночные пары Solana из DexScreener.
Реальная торговля, кошелёк, автоматическая рассылка, платёжная система и полноценная проверка Rug Pull НЕ реализованы.

Размещение:
1. Создайте новый публичный или приватный репозиторий GitHub.
2. Загрузите в корень репозитория все 4 файла из этого архива: main.py, requirements.txt, render.yaml, README_RU.txt.
3. На Render выберите New > Blueprint и подключите этот GitHub-репозиторий, либо создайте Web Service с Build Command:
   pip install -r requirements.txt
   и Start Command:
   uvicorn main:app --host 0.0.0.0 --port $PORT
4. После успешного deploy проверьте адрес https://ВАШ-СЕРВИС.onrender.com/api/health
   Ожидаемый ответ: {"status":"ok","mode":"paper"}

План Free подходит для тестирования, но Render выключает бесплатный сервис после 15 минут без запросов; для постоянной работы 24/7 нужен платный тариф.
Никогда не добавляйте seed-фразу или приватный ключ в GitHub, Render variables или этот проект.
