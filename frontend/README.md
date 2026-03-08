# Translation Viewer Frontend

Simple web interface for viewing live translations.

## Features

- Select language (Swedish, Norwegian, German)
- Real-time translation display via WebSocket
- Auto-play translated audio
- Shows last 10 translations
- Mobile-friendly responsive design

## Usage

### Option 1: Open Directly (Same Computer as Backend)

```bash
# Just open in browser
open frontend/index.html
```

Backend URL is `localhost:8000` by default.

### Option 2: Serve via Backend (Recommended)

The backend can serve the frontend:

1. Copy `index.html` to backend directory:
```bash
cp frontend/index.html backend/static/
```

2. Access at: `http://MAC_MINI_IP:8000/static/index.html`

### Option 3: Simple HTTP Server

```bash
cd frontend
python3 -m http.server 8080
```

Then open: `http://localhost:8080`

**Important:** Edit `index.html` and change `BACKEND_URL`:
```javascript
const BACKEND_URL = 'ws://YOUR_MAC_MINI_IP:8000';
```

## Configuration

Edit `index.html` line 195:

```javascript
const BACKEND_URL = 'ws://192.168.1.100:8000';  // Your Mac Mini IP
```

## Testing

1. **Start backend** on Mac Mini
2. **Open frontend** in browser
3. **Click language button** (Swedish/Norwegian/German)
4. **Should see**: "🟢 Connected - Listening for Swedish"
5. **Have Pi send audio**
6. **Watch translations appear** and audio auto-play

## Troubleshooting

### "Cannot connect to backend"

**Check 1: Is backend URL correct?**
```javascript
// In index.html, line 195
const BACKEND_URL = 'ws://MAC_MINI_IP:8000';  // Must match your Mac Mini
```

**Check 2: Is backend running?**
```bash
curl http://MAC_MINI_IP:8000
```

**Check 3: Mixed content (if using HTTPS)?**
If your site is HTTPS, you need WSS (secure WebSocket):
```javascript
const BACKEND_URL = 'wss://MAC_MINI_IP:8000';
```

### Audio doesn't auto-play

Modern browsers block auto-play until user interacts with page.

**Solution**: Click anywhere on page first, then it will work.

### Connection keeps dropping

Check firewall settings on Mac Mini - port 8000 must be open.

### Translations not appearing

**Check browser console** (F12 → Console tab) for errors.

Common issues:
- Wrong backend URL
- WebSocket connection failed
- CORS errors (backend should have CORS enabled)

## Browser Compatibility

Tested on:
- ✅ Chrome/Edge (recommended)
- ✅ Firefox
- ✅ Safari
- ✅ Mobile browsers (iOS Safari, Chrome Android)

## Mobile Access

1. Make sure your phone is on **same WiFi** as Mac Mini
2. Find Mac Mini IP: `ifconfig | grep "inet "`
3. On phone, open: `http://MAC_MINI_IP:8000/static/index.html`
4. Select language and listen!

## Next Steps (Phase 3)

- [ ] Convert to React app
- [ ] Add user authentication
- [ ] Session selection (multiple events)
- [ ] Historical playback
- [ ] Export transcripts
- [ ] Settings panel
- [ ] Theme customization
