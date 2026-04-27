# JAWL MiniMax Settings (correct as of 2026-04-27)

## .env
```
LLM_API_URL="https://api.minimax.io/v1"
LLM_API_KEY_1="sk-cp-JqkXlcj0NLALaq1zVJM33J4mwMs2U-Lj5lv3y3Ai2WTCDOB-JNwtjxAWeGSP8TL3jmsHVQ4aWS7u6j4uyVz8e9P_iX5gKm0fi1qUpx3npuwLXP1cQ9BQDzg"
AIOGRAM_BOT_TOKEN="8756002507:AAFtrcDElw9kJUg7hFUZAsQKoMjzF1gzJcI"
```

## settings.yaml
```yaml
model_name: "minimax-m2.7" # MiniMax-M2
is_multimodal: false
```

## To restore after restart:
```bash
cp /home/rem/JAWL/.env.minimax-backup /home/rem/JAWL/.env
# (already has correct settings)
```
