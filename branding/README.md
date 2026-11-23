# FluteSpheric channel branding

Channel: **FluteSpheric** — `@flutespheric` (handle checked free on YouTube)
Channel ID and OAuth token: **pending** until the channel is created.

| Asset | File | Status |
| --- | --- | --- |
| Banner | `channel_banner_2048x1152.jpg` | ready to upload |
| Description | `channel_description.txt` | ready to set |
| Keywords | `channel_keywords.txt` | ready to set |
| Avatar | `channel_avatar_800.png` | **manual upload only** |
| Video watermark | not used | the sheet-music panel owns the upper-right corner |

## Create the channel and token

1. Create the FluteSpheric channel in YouTube Studio with the free
   `@flutespheric` handle.
2. Mint a refresh token for that channel with the existing OAuth client:

```powershell
python scripts/get_refresh_token.py `
  --client-json "D:\E agy cli\E api tokens\.HarpSpheric\token_HarpSpheric.json" `
  --out "D:\E agy cli\E api tokens\.FluteSpheric\token_FluteSpheric.json" `
  --nickname FluteSpheric
```

The client JSON only supplies the shared `client_id`/`client_secret`; the
script opens Google's consent screen so you can pick the new FluteSpheric
channel and writes a channel-specific refresh token.

## What the YouTube Data API can set

`channels.update` accepts only `brandingSettings`, `invideoPromotion` and
`localizations`. For branding it can change the description, keywords, country,
default language, trailer and banner. The banner is a two-step flow:

1. `channelBanners.insert` uploads the image (16:9, at least 2048x1152).
2. `channels.update` stores the returned URL in
   `brandingSettings.image.bannerExternalUrl`.

```powershell
python scripts/update_channel_branding.py `
  --token-json "D:\E agy cli\E api tokens\.FluteSpheric\token_FluteSpheric.json" `
  --confirm-channel FluteSpheric --apply
```

## What it cannot set

- **Channel avatar.** There is no `channels.avatar` method and no avatar field
  in `brandingSettings.image`. Upload `channel_avatar_800.png` in YouTube Studio
  → Customisation → Branding.
- **Channel title.** `channels.update` returns `channelTitleUpdateForbidden`
  when the title changes; set it when creating the channel.
- **Handle / custom URL and links.** Studio-only settings.

Reference: <https://developers.google.com/youtube/v3/docs/channels/update>
