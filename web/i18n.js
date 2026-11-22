'use strict';
// Ulak i18n: tiny dictionary + t(). Language: saved choice, else browser language (tr -> tr, anything else -> en).
const I18N = {
  en: {
    appName: 'Ulak', st_blocked: 'Needs you', st_working: 'Working', st_done: 'Done', st_idle: 'Idle', st_unknown: 'Status unknown',
    g_pinned: 'Pinned', g_blocked: 'Needs you', g_working: 'Working', g_done: 'Just finished', g_unknown: 'Status unknown', g_idle: 'Idle',
    show: 'Show', hide: 'Hide', justNow: 'just now', min: 'm', hour: 'h', day: 'd',
    doneAgo: 'Done · {t}', summaryBlocked: '<b>{n}</b> waiting for you', summaryUnknown: 'Unknown status: {n}', summaryCalm: '{n} working · all calm',
    empty: 'No sessions yet.<br>Start one below.', openSession: 'Open {project}', pin: 'Pin', unpin: 'Unpin', pinFail: 'Could not save the pin',
    deny: 'Deny', approve: 'Approve', approved: 'Approved', denied: 'Denied', sent: 'Sent', replySent: 'Reply sent',
    offline: 'No connection', reconnecting: 'Reconnecting…', enableBell: 'Turn on notifications', history: 'Notification history',
    refresh: 'Refresh', refreshed: 'Refreshed', pullRelease: 'Release to refresh', pull: 'Pull…',
    quickSend: 'Quick send', quickTo: 'Send to {x}', quickPh: 'A note, task or question…', cancel: 'Cancel', send: 'Send',
    newSession: '+ New session', newTitle: 'New session', folderSearch: 'Search folders…', noFolder: 'No matching folder', defaultModel: 'default', modelLabel: 'Model', start: 'Start', starting: 'Session starting',
    back: 'Back', menu: 'Menu', keys: 'Keys', attach: 'Attach file', attached: 'File attached. Add a note and send.', composerPh: 'Write to the session…', sendAria: 'Send',
    loading: 'Loading…', noChat: 'Nothing said yet.', tool: 'tool', working: 'Working…', newMsgs: '↓ New',
    closedSession: 'This session has closed.', backToList: 'Back to list',
    waitingApproval: 'Waiting for approval', destructive: '⚠ destructive', cutNote: 'The full command does not fit on screen, so it can only be denied.',
    alwaysAllow: 'Always allow for this session', unknownScreen: 'Screen not recognised', unknownHelp: 'Use the key bar below or answer in the terminal.',
    filePicker: 'File picker is open', closeEsc: 'Close (Esc)', trustTitle: 'Folder trust', trustQ: 'Trust this folder? {f}', question: 'Question', cont: 'Continue →',
    nextBlocked: 'Next session waiting', irreversible: '⚠ This may not be reversible', irreversibleP: 'This command can affect the outside world or files. Full command:', yesRun: 'Yes, run it',
    refreshSession: 'Refresh', closeSession: 'Close session…', closeAsk: 'Close this session?', closeWorking: ' is working right now; unfinished work may be lost.', closeIdle: ' will be closed.', cannotUndo: ' This cannot be undone.', yesClose: 'Yes, close', closed: 'Closed',
    notifications: 'Notifications', noNotifs: 'No notifications yet', bellOn: 'Notifications on', noPermission: 'Notification permission was not granted', noPushKey: 'No push key on the server',
    needsYouTitle: '{p} · needs you', doneTitle: '{p} · done', askBody: 'Waiting for approval', finishedBody: 'Finished',
    language: 'Language', limits: 'Limits', limitsTitle: 'Usage limits', resets: 'resets {t}', resetsIn: 'in {t}', used: '{n}% used', stale: 'not updated for {t}', noLimits: 'No limit data configured.', left: '{n}% left',
    pane: 'pane', settings: 'Settings', install: 'Add to Home Screen for push notifications',
  },
  tr: {
    appName: 'Ulak', st_blocked: 'Seni bekliyor', st_working: 'Çalışıyor', st_done: 'Bitti', st_idle: 'Boşta', st_unknown: 'Durum bilinmiyor',
    g_pinned: 'Sabitlenenler', g_blocked: 'Seni bekleyenler', g_working: 'Çalışıyor', g_done: 'Yeni bitenler', g_unknown: 'Durumu bilinmeyenler', g_idle: 'Boşta',
    show: 'Göster', hide: 'Gizle', justNow: 'az önce', min: 'dk', hour: 'sa', day: 'g',
    doneAgo: 'Bitti · {t}', summaryBlocked: '<b>{n}</b> oturum seni bekliyor', summaryUnknown: 'Durumu bilinmeyen oturum: {n}', summaryCalm: '{n} çalışıyor · hepsi sakin',
    empty: 'Henüz oturum yok.<br>Aşağıdan yeni bir tane başlat.', openSession: '{project} oturumunu aç', pin: 'Sabitle', unpin: 'Sabitlemeyi kaldır', pinFail: 'Sabitleme kaydedilemedi',
    deny: 'Reddet', approve: 'Onayla', approved: 'Onaylandı', denied: 'Reddedildi', sent: 'Gönderildi', replySent: 'Yanıt gönderildi',
    offline: 'Bağlantı yok', reconnecting: 'Yeniden bağlanıyor…', enableBell: 'Bildirimleri aç', history: 'Bildirim geçmişi',
    refresh: 'Yenile', refreshed: 'Yenilendi', pullRelease: 'Bırak, yenilensin', pull: 'Çek…',
    quickSend: 'Hızlı yaz', quickTo: 'Yaz: {x}', quickPh: 'Not, görev ya da soru…', cancel: 'Vazgeç', send: 'Gönder',
    newSession: '+ Yeni oturum', newTitle: 'Yeni oturum', folderSearch: 'Klasör ara…', noFolder: 'Eşleşen klasör yok', defaultModel: 'varsayılan', modelLabel: 'Model', start: 'Başlat', starting: 'Oturum başlıyor',
    back: 'Geri', menu: 'Menü', keys: 'Tuşlar', attach: 'Dosya ekle', attached: 'Dosya eklendi. Not yazıp gönder.', composerPh: 'Oturuma yaz…', sendAria: 'Gönder',
    loading: 'Yükleniyor…', noChat: 'Henüz konuşma yok.', tool: 'araç', working: 'Çalışıyor…', newMsgs: '↓ Yeni',
    closedSession: 'Bu oturum kapandı.', backToList: 'Listeye dön',
    waitingApproval: 'Onay bekliyor', destructive: '⚠ yıkıcı', cutNote: 'Komutun tamamı ekrana sığmıyor; güvenlik için yalnız reddedilebilir.',
    alwaysAllow: 'Bu oturum için hep izin ver', unknownScreen: 'Ekran tanınmadı', unknownHelp: 'Aşağıdaki tuş çubuğunu kullan ya da terminalden yanıtla.',
    filePicker: 'Dosya seçici açık', closeEsc: 'Kapat (Esc)', trustTitle: 'Klasör güveni', trustQ: 'Bu klasöre güvenilsin mi? {f}', question: 'Soru', cont: 'Devam →',
    nextBlocked: 'Sıradaki bekleyen oturum', irreversible: '⚠ Geri alınamayabilir', irreversibleP: 'Bu komut dışarıyı ya da dosyaları etkileyebilir. Tam komut:', yesRun: 'Evet, çalıştır',
    refreshSession: 'Yenile', closeSession: 'Oturumu kapat…', closeAsk: 'Oturum kapatılsın mı?', closeWorking: ' şu an çalışıyor; yarım iş kaybolabilir.', closeIdle: ' kapanacak.', cannotUndo: ' Geri alınamaz.', yesClose: 'Evet, kapat', closed: 'Kapatıldı',
    notifications: 'Bildirimler', noNotifs: 'Henüz bildirim yok', bellOn: 'Bildirimler açık', noPermission: 'Bildirim izni verilmedi', noPushKey: 'Sunucuda push anahtarı yok',
    needsYouTitle: '{p} · onay bekliyor', doneTitle: '{p} · bitti', askBody: 'Onay bekliyor', finishedBody: 'İş bitti',
    language: 'Dil', limits: 'Limitler', limitsTitle: 'Kullanım limitleri', resets: 'sıfırlanır {t}', resetsIn: '{t} sonra', used: '%{n} kullanıldı', stale: '{t} önce güncellendi', noLimits: 'Limit verisi ayarlanmamış.', left: '%{n} kaldı',
    pane: 'panel', settings: 'Ayarlar', install: 'Bildirim için ana ekrana ekle',
  },
};
// Server error strings (English on the wire) -> Turkish.
const ERR_TR = {
  'Unsupported file type': 'Desteklenmeyen dosya türü', 'File too large (15 MB max)': 'Dosya çok büyük (en çok 15 MB)',
  'Session is closed or unknown': 'Oturum kapalı ya da bilinmiyor', 'Message is empty': 'Mesaj boş', 'Quick-send target not found': 'Hedef oturum bulunamadı',
  'Session is no longer waiting for approval': 'Oturum artık onay beklemiyor', 'No recognised approval on screen; use the terminal': 'Ekranda tanınan bir onay yok; terminali kullan',
  'The approval screen changed; refresh': 'Onay ekranı değişti; yenile', 'This prompt has no such option': 'Bu istemde böyle bir seçenek yok',
  'A reply is already in progress': 'Bir yanıt zaten gönderiliyor', 'The confirmation is no longer valid; try again': 'İkinci onayın süresi doldu veya ekran değişti. Yeniden dene.',
  'The full command is not visible; it can only be denied': 'Komutun tamamı görünmüyor; yalnız reddedilebilir',
  'Session-wide allow is disabled for destructive commands': 'Yıkıcı komutta oturum boyu izin kapalı', 'Session is no longer waiting for anything': 'Oturum artık bir şey beklemiyor',
  'No menu to choose from on screen': 'Ekranda seçilecek bir menü yok', 'The screen changed; refresh': 'Ekran değişti; yenile', 'No such option': 'Böyle bir seçenek yok',
  'Invalid key': 'Geçersiz tuş', 'Destructive command: approve from the approval card (two steps)': 'Yıkıcı komut: onay kartından iki adımla onayla',
  'Invalid tool or model': 'Araç ya da model geçersiz', 'Invalid folder': 'Klasör geçersiz', 'Invalid pin list': 'Geçersiz pin listesi', 'Tailscale identity required': 'Tailscale kimliği gerekli',
};
const detectLang = () => {
  let saved = null; try { saved = localStorage.getItem('lang'); } catch {}
  if (saved === 'tr' || saved === 'en') return saved;
  return /^tr\b/i.test(navigator.language || '') ? 'tr' : 'en';
};
let LANG = detectLang();
const t = (key, vars) => {
  let s = (I18N[LANG] && I18N[LANG][key]) ?? I18N.en[key] ?? key;
  if (vars) for (const k in vars) s = s.replaceAll(`{${k}}`, vars[k]);
  return s;
};
const errText = m => (LANG === 'tr' && ERR_TR[m]) || m;
function applyStaticI18n() {
  document.documentElement.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-aria]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
}
