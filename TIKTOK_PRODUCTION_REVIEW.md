# TikTok Production Review — Shorts Studio

## Готово

- Production-конфигурация импортирована из Sandbox.
- Подключены Login Kit и Content Posting API с Direct Post.
- Используются `user.info.basic`, `video.publish` и `video.upload`.
- Перед каждой публикацией приложение заново запрашивает `creator_info`.
- Пользователь видит предпросмотр, редактирует название и хэштеги, вручную выбирает видимость и подтверждает права/музыку.
- Комментарии, Duet и Stitch по умолчанию выключены и блокируются, если их запретил аккаунт.
- Приложение отслеживает итоговый статус публикации и не подменяет публичную публикацию приватной.

## Текст для поля App review

Shorts Studio is a desktop creator workflow for media the creator owns or is authorized to use. Login Kit connects the selected TikTok account using OAuth scopes user.info.basic and video.publish. Before each Direct Post, the app queries creator_info and shows the current nickname, available privacy options, interaction restrictions and maximum duration. The creator previews the rendered video, edits title and hashtags, manually selects privacy, chooses comment, duet and stitch settings, completes commercial-content disclosures, confirms TikTok Music Usage, and explicitly approves upload. Media is sent via FILE_UPLOAD. The app polls publish/status/fetch and displays processing, completion or failure. No default privacy is selected and no silent fallback occurs.

## Осталось до отправки на ревью

1. Сделать официальный сайт Shorts Studio общедоступным.
2. Указать публичные адреса сайта, Privacy Policy и Terms of Service.
3. Указать реальный контактный email поддержки.
4. Загрузить иконку `shorts-studio-icon.png` (1024 x 1024, PNG).
5. Загрузить демонстрационное видео MP4/MOV до 50 MB с полным Sandbox-сценарием.
6. Нажать Submit for review и дождаться одобрения TikTok.

Без одобрения TikTok Direct Post API разрешает только `SELF_ONLY`; приложение намеренно не обходит это ограничение и не делает скрытый приватный пост вместо публичного.
