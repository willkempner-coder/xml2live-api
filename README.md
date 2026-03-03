# XML2LIVE API starter

This repo is the minimal backend for the `willkempner.com/xml2live` page.

## Files

- `api/xml2live.py` - Vercel  serverless endpoint
- `scripts/` - converter logic copied from the desktop app
- `Template/` - Ableton 11 and Ableton 9 templates
- `requirements.txt` - Python dependency list
- `vercel.json` - Vercel runtime config

## Deploy

1. Put these files in your backend GitHub repo.
2. Import that repo into Vercel.
3. Deploy.
4. Copy the deployed URL.
5. In your website repo, edit:
   - `xml2live/config.js`
6. Set:

```js
window.XML2LIVE_API_URL = "https://YOUR-VERCEL-URL.vercel.app/api/xml2live";
```

7. Push the website repo.

## Notes

- This backend does not consolidate media.
- It writes the XML's original file paths into the generated `.als`.
- It returns a zip containing the generated Ableton project folder.
