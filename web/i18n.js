'use strict';
// Ulak i18n: tiny dictionary + t(). Language: saved choice, else browser language (tr -> tr, anything else -> en).
const I18N = {
  en: {
    appName: 'Ulak', st_blocked: 'Needs you', st_working: 'Working', st_done: 'Done', st_idle: 'Idle', st_unknown: 'Status unknown',
    g_pinned: 'Pinned', g_blocked: 'Needs you', g_working: 'Working', g_done: 'Just finished', g_unknown: 'Status unknown', g_idle: 'Idle',
    show: 'Show', hide: 'Hide', justNow: 'just now', min: 'm', hour: 'h', day: 'd',
    doneAgo: 'Done · {t}', summaryBlocked: '<b>{n}</b> waiting for you', summaryUnknown: 'Unknown status: {n}', summaryCalm: '{n} working · all calm',
    empty: 'No sessions yet.<br>Start one below.', openSession: 'Open {project}', pin: 'Pin', unpin: 'Unpin', pinFail: 'Could not save the pin',
    deny: 'Deny', approve: 'Approve', approved: 'Approved', denied: 'Denied', chose: 'Chosen', keysSent: 'Keys sent', sent: 'Sent', replySent: 'Reply sent', allowedSession: 'Allowed for this session', rcptArrived: 'agent received it {t}', rcptPending: 'on its way', rcptUnknown: 'not seen on the agent\'s screen', loadEarlier: 'Load earlier', conversations: 'Earlier conversations', conversationsNote: 'Read only. What this folder talked about before.', noConversations: 'No earlier conversation in this folder', mailSending: 'Sending…', mailSent: 'Sent · waiting for the agent', mailUnseen: 'Not in the conversation yet', mailUnsent: 'Not sent', mailRetry: 'Try again', mailDrop: 'Delete',
    decisions: 'Decisions', noDecisions: 'No decisions yet', outRefused: 'Refused', outConfirm: 'Second step asked', outTimeout: 'Denied after {n} min without an answer', timeoutOn: 'Unanswered approvals are denied after {n} min',
    replyUnverified: 'Reply sent. Ulak could not verify a screen change.', reviewInSession: 'Open the session to review this approval.',
    offline: 'No connection', reconnecting: 'Reconnecting…', syncing: 'Refreshing sessions…', enableBell: 'Turn on notifications', history: 'Notification history',
    refresh: 'Refresh', refreshed: 'Refreshed', pullRelease: 'Release to refresh', pull: 'Pull…',
    quickSend: 'Quick send', quickTo: 'Send to {x}', quickPh: 'A note, task or question…', cancel: 'Cancel', send: 'Send',
    newSession: '+ New session', newTitle: 'New session', folderSearch: 'Search folders…', noFolder: 'No matching folder', defaultModel: 'default', modelLabel: 'Model', start: 'Start', starting: 'Session starting',
    plainPane: 'This pane has no agent in it. Ulak does not type into a plain shell; the menu still shows its screen.',
    back: 'Back', menu: 'Menu', keys: 'Keys', attach: 'Attach file', attached: 'File attached. Add a note and send.', attachedKept: 'File attached. Where and with what this photo was taken may still be in it.', composerPh: 'Write to the session…', sendAria: 'Send', slashAria: 'Slash commands',
    loading: 'Loading…', noChat: 'Nothing said yet.', tool: 'tool', subagent: 'Subagent', subRunning: 'Working', subDone: 'Done', subFailed: 'Failed', working: 'Working…', newMsgs: '↓ New',
    closedSession: 'This session has closed.', backToList: 'Back to list',
    chips: 'Quick replies', chipAdd: 'Add', chipPh: 'A short text of your own…', chipUp: 'Move up', chipDown: 'Move down', chipDrop: 'Delete', chipsEmpty: 'None yet. What you add waits above the message box. A tap puts it in your draft; nothing is sent until you send it.', storageOff: 'This browser is not keeping data, so these last only until the page closes.',
    search: 'Search', searchPh: 'A word or a phrase…', searchNone: 'Nothing found.', searchPartial: 'Older messages were not searched: it took too long.', searchClosed: 'This session has closed.', roleYou: 'You', roleAgent: 'Agent',
    waitingApproval: 'Waiting for approval', destructive: 'Needs two seals', review: 'Review…', twoSealsNote: 'You confirm once more on the next screen. That confirmation expires in 2 minutes.', cutNote: 'The full command does not fit on screen, so it can only be denied.', hiddenNote: 'This command has characters you cannot see: {c}. They are written out where they stand.', hiddenShort: 'Has characters you cannot see: {c}',
    alwaysAllow: 'Always allow for this session', otherAnswers: 'Other answers', alwaysNote: 'The agent will stop asking about this kind of command until the session ends.', close: 'Close', bellNone: 'Notifications are not available in this browser', unknownScreen: 'Screen not recognised', unknownHelp: 'Use the key bar below or answer in the terminal.',
    filePicker: 'File picker is open', closeEsc: 'Close (Esc)', trustTitle: 'Folder trust', trustQ: 'Trust this folder? {f}', question: 'Question', cont: 'Continue →',
    nextBlocked: 'Next session waiting', irreversible: 'This may not be reversible', irreversibleP: 'This command can affect the outside world or files. Full command:', yesRun: 'Yes, run it',
    changes: 'Changes', changesNote: 'Compared with the last commit. Read-only.', changesBranch: 'On {b}, compared with the last commit. Read-only.', notRepo: 'This folder is not a git repository.', nothingChanged: 'Nothing has changed since the last commit.', onlyNew: 'New files are listed above; their contents are not shown.', moreFiles: 'and {n} more', diffCut: 'The diff is long. Only the first part is shown.', changesFailed: 'Could not read the changes.',
    agentScreen: 'Agent screen', agentScreenNote: 'What the terminal shows right now. Read-only.', refreshSession: 'Refresh', closeSession: 'Close session…', closeAsk: 'Close this session?', closeWorking: ' is working right now; unfinished work may be lost.', closeIdle: ' will be closed.', cannotUndo: ' This cannot be undone.', yesClose: 'Yes, close', closed: 'Closed',
    notifications: 'Notifications', noNotifs: 'No notifications yet', bellOn: 'Notifications on', bellReached: 'reached this phone {t} ago', bellStopped: 'Notifications stopped on this device. Turn them on again', bellDenied: "Notifications are turned off for Ulak in this device's settings", bellTest: 'Send a test notification', bellTestSent: 'Test sent. It should arrive in a moment', bellTestFailed: 'The push service refused the test ({n})', noPermission: 'Notification permission was not granted', noPushKey: 'No push key on the server',
    needsYouTitle: '{p} · needs you', doneTitle: '{p} · done', askBody: 'Waiting for approval', finishedBody: 'Finished',
    copy: 'Copy', copied: 'Copied', copyFail: 'Could not copy', openImage: 'Open picture {x}',
    ctxShort: 'Context {n}', cacheShort: 'Cache {n}', cacheMins: '~{n}m', cacheCold: 'Cache cold', win5h: '5h', win7d: 'wk', metersTitle: 'This session', mContext: 'Context', tokens: '{n} tokens', mCache: 'Cache', cache1h: '1 hour', cache5m: '5 minute', cacheLeft: '~{n}m left', fromRecord: 'from the session record', agoT: '{t} ago', limFile: 'account-wide, from your limits file', limCodex: 'account-wide, from Codex records or your limits file',
    kRead: '{n} read|{n} reads', kSearch: '{n} search|{n} searches', kEdit: '{n} edit|{n} edits', kCommand: '{n} command|{n} commands', kPage: '{n} page|{n} pages', kOther: '{n} other job|{n} other jobs', runNow: 'Running: {x}', noResult: 'No result in the record', failedCall: 'Failed', rejectedCall: 'You said no', didNotRun: 'It did not run.', editCut: 'Diff cut short', wrote: '{n} line written|{n} lines written',
    language: 'Language', darkTheme: 'Dark theme', appearance: 'Appearance', themeDark: 'Dark', themeLight: 'Light', limits: 'Limits', limitsTitle: 'Usage limits', resets: 'resets {t}', resetsIn: 'in {t}', used: '{n}% used', stale: 'not updated for {t}', readAgo: 'read {t} ago', noLimits: 'No limit data configured.', left: '{n}% left',
    pane: 'pane', settings: 'Settings', install: 'Add to Home Screen for push notifications',
  },
  tr: {
    appName: 'Ulak', st_blocked: 'Seni bekliyor', st_working: 'Çalışıyor', st_done: 'Bitti', st_idle: 'Boşta', st_unknown: 'Durum bilinmiyor',
    g_pinned: 'Sabitlenenler', g_blocked: 'Seni bekleyenler', g_working: 'Çalışıyor', g_done: 'Yeni bitenler', g_unknown: 'Durumu bilinmeyenler', g_idle: 'Boşta',
    show: 'Göster', hide: 'Gizle', justNow: 'az önce', min: 'dk', hour: 'sa', day: 'g',
    doneAgo: 'Bitti · {t}', summaryBlocked: '<b>{n}</b> oturum seni bekliyor', summaryUnknown: 'Durumu bilinmeyen oturum: {n}', summaryCalm: '{n} çalışıyor · hepsi sakin',
    empty: 'Henüz oturum yok.<br>Aşağıdan yeni bir tane başlat.', openSession: '{project} oturumunu aç', pin: 'Sabitle', unpin: 'Sabitlemeyi kaldır', pinFail: 'Sabitleme kaydedilemedi',
    deny: 'Reddet', approve: 'Onayla', approved: 'Onaylandı', denied: 'Reddedildi', chose: 'Seçildi', keysSent: 'Tuş gönderildi', sent: 'Gönderildi', replySent: 'Yanıt gönderildi', allowedSession: 'Oturum için izin verildi', rcptArrived: 'ajan aldı {t}', rcptPending: 'yolda', rcptUnknown: 'ajanın ekranında görülmedi', loadEarlier: 'Daha eskisini yükle', conversations: 'Önceki sohbetler', conversationsNote: 'Salt okunur. Bu klasörde daha önce konuşulanlar.', noConversations: 'Bu klasörde önceki sohbet yok', mailSending: 'Gönderiliyor…', mailSent: 'Gönderildi · ajan bekleniyor', mailUnseen: 'Sohbette henüz görünmedi', mailUnsent: 'Gitmedi', mailRetry: 'Tekrar dene', mailDrop: 'Sil',
    decisions: 'Kararlar', noDecisions: 'Henüz karar yok', outRefused: 'Geri çevrildi', outConfirm: 'İkinci adım istendi', outTimeout: '{n} dk yanıt gelmeyince reddedildi', timeoutOn: 'Yanıtsız onaylar {n} dk sonra reddedilir',
    replyUnverified: 'Yanıt gönderildi. Ekran değişikliği doğrulanamadı.', reviewInSession: 'Bu onayı incelemek için oturumu aç.',
    offline: 'Bağlantı yok', reconnecting: 'Yeniden bağlanıyor…', syncing: 'Oturumlar yenileniyor…', enableBell: 'Bildirimleri aç', history: 'Bildirim geçmişi',
    refresh: 'Yenile', refreshed: 'Yenilendi', pullRelease: 'Bırak, yenilensin', pull: 'Çek…',
    quickSend: 'Hızlı yaz', quickTo: 'Yaz: {x}', quickPh: 'Not, görev ya da soru…', cancel: 'Vazgeç', send: 'Gönder',
    newSession: '+ Yeni oturum', newTitle: 'Yeni oturum', folderSearch: 'Klasör ara…', noFolder: 'Eşleşen klasör yok', defaultModel: 'varsayılan', modelLabel: 'Model', start: 'Başlat', starting: 'Oturum başlıyor',
    plainPane: 'Bu panelde ajan yok. Ulak düz kabuğa yazmaz; ekranını menüden okuyabilirsin.',
    back: 'Geri', menu: 'Menü', keys: 'Tuşlar', attach: 'Dosya ekle', attached: 'Dosya eklendi. Not yazıp gönder.', attachedKept: 'Dosya eklendi. Fotoğrafın nerede ve neyle çekildiği içinde kalmış olabilir.', composerPh: 'Oturuma yaz…', sendAria: 'Gönder', slashAria: 'Eğik çizgi komutları',
    loading: 'Yükleniyor…', noChat: 'Henüz konuşma yok.', tool: 'araç', subagent: 'Alt ajan', subRunning: 'Çalışıyor', subDone: 'Bitti', subFailed: 'Başarısız', working: 'Çalışıyor…', newMsgs: '↓ Yeni',
    closedSession: 'Bu oturum kapandı.', backToList: 'Listeye dön',
    chips: 'Hazır yanıtlar', chipAdd: 'Ekle', chipPh: 'Kendi kısa metnin…', chipUp: 'Yukarı taşı', chipDown: 'Aşağı taşı', chipDrop: 'Sil', chipsEmpty: 'Henüz yok. Eklediklerin yazma kutusunun üstünde durur. Dokununca taslağa yazılır; sen göndermeden hiçbir şey gitmez.', storageOff: 'Bu tarayıcı veri saklamıyor; bunlar yalnız sayfa kapanana kadar durur.',
    search: 'Ara', searchPh: 'Bir kelime ya da ifade…', searchNone: 'Bir şey bulunamadı.', searchPartial: 'Daha eski mesajlar aranmadı: çok uzun sürdü.', searchClosed: 'Bu oturum kapandı.', roleYou: 'Sen', roleAgent: 'Ajan',
    waitingApproval: 'Onay bekliyor', destructive: 'İki mühür ister', review: 'İncele…', twoSealsNote: 'Sonraki ekranda bir kez daha onaylarsın. O onay 2 dakika geçerlidir.', cutNote: 'Komutun tamamı ekrana sığmıyor; güvenlik için yalnız reddedilebilir.', hiddenNote: 'Bu komutta göremeyeceğin karakterler var: {c}. Bulundukları yerde açık yazıldı.', hiddenShort: 'Göremeyeceğin karakterler var: {c}',
    alwaysAllow: 'Bu oturum için hep izin ver', otherAnswers: 'Diğer cevaplar', alwaysNote: 'Ajan, oturum bitene kadar bu tür komutları sormadan çalıştırır.', close: 'Kapat', bellNone: 'Bu tarayıcıda bildirim kullanılamıyor', unknownScreen: 'Ekran tanınmadı', unknownHelp: 'Aşağıdaki tuş çubuğunu kullan ya da terminalden yanıtla.',
    filePicker: 'Dosya seçici açık', closeEsc: 'Kapat (Esc)', trustTitle: 'Klasör güveni', trustQ: 'Bu klasöre güvenilsin mi? {f}', question: 'Soru', cont: 'Devam →',
    nextBlocked: 'Sıradaki bekleyen oturum', irreversible: 'Geri alınamayabilir', irreversibleP: 'Bu komut dışarıyı ya da dosyaları etkileyebilir. Tam komut:', yesRun: 'Evet, çalıştır',
    changes: 'Değişiklikler', changesNote: 'Son commit ile karşılaştırma. Salt okunur.', changesBranch: '{b} dalında, son commit ile karşılaştırma. Salt okunur.', notRepo: 'Bu klasör bir git deposu değil.', nothingChanged: 'Son commit\'ten beri değişiklik yok.', onlyNew: 'Yeni dosyalar yukarıda; içerikleri gösterilmez.', moreFiles: 've {n} dosya daha', diffCut: 'Diff uzun. Yalnız ilk kısmı gösteriliyor.', changesFailed: 'Değişiklikler okunamadı.',
    agentScreen: 'Ajan ekranı', agentScreenNote: 'Terminalde şu an görünen. Salt okunur.', refreshSession: 'Yenile', closeSession: 'Oturumu kapat…', closeAsk: 'Oturum kapatılsın mı?', closeWorking: ' şu an çalışıyor; yarım iş kaybolabilir.', closeIdle: ' kapanacak.', cannotUndo: ' Geri alınamaz.', yesClose: 'Evet, kapat', closed: 'Kapatıldı',
    notifications: 'Bildirimler', noNotifs: 'Henüz bildirim yok', bellOn: 'Bildirimler açık', bellReached: '{t} önce bu telefona ulaştı', bellStopped: 'Bildirimler bu cihazda durdu. Yeniden aç', bellDenied: 'Ulak bildirimleri bu cihazın ayarlarından kapalı', bellTest: 'Deneme bildirimi gönder', bellTestSent: 'Deneme gönderildi. Birazdan gelmeli', bellTestFailed: 'Bildirim servisi denemeyi reddetti ({n})', noPermission: 'Bildirim izni verilmedi', noPushKey: 'Sunucuda push anahtarı yok',
    needsYouTitle: '{p} · onay bekliyor', doneTitle: '{p} · bitti', askBody: 'Onay bekliyor', finishedBody: 'İş bitti',
    copy: 'Kopyala', copied: 'Kopyalandı', copyFail: 'Kopyalanamadı', openImage: '{x} resmini aç',
    ctxShort: 'Bağlam {n}', cacheShort: 'Cache {n}', cacheMins: '~{n} dk', cacheCold: 'Cache soğudu', win5h: '5s', win7d: 'hf', metersTitle: 'Bu oturum', mContext: 'Bağlam', tokens: '{n} belirteç', mCache: 'Cache', cache1h: '1 saat', cache5m: '5 dakika', cacheLeft: '~{n} dk kaldı', fromRecord: 'oturum kaydından', agoT: '{t} önce', limFile: 'hesap geneli, limit dosyandan', limCodex: 'hesap geneli, Codex kayıtlarından ya da limit dosyandan',
    kRead: '{n} okuma', kSearch: '{n} arama', kEdit: '{n} düzenleme', kCommand: '{n} komut', kPage: '{n} sayfa', kOther: '{n} diğer iş', runNow: 'Çalışıyor: {x}', noResult: 'Kayıtta sonuç yok', failedCall: 'Hata', rejectedCall: 'Sen reddettin', didNotRun: 'Çalışmadı.', editCut: 'Fark kısaltıldı', wrote: '{n} satır yazıldı',
    language: 'Dil', darkTheme: 'Koyu tema', appearance: 'Görünüm', themeDark: 'Koyu', themeLight: 'Açık', limits: 'Limitler', limitsTitle: 'Kullanım limitleri', resets: 'sıfırlanır {t}', resetsIn: '{t} sonra', used: '%{n} kullanıldı', stale: '{t} önce güncellendi', readAgo: '{t} önce okundu', noLimits: 'Limit verisi ayarlanmamış.', left: '%{n} kaldı',
    pane: 'panel', settings: 'Ayarlar', install: 'Bildirim için ana ekrana ekle',
  },
};
// Server error strings (English on the wire) -> Turkish.
const ERR_TR = {
  'Unsupported file type': 'Desteklenmeyen dosya türü', 'File too large (15 MB max)': 'Dosya çok büyük (en çok 15 MB)',
  'No agent is running in this pane; Ulak does not type into a plain shell': 'Bu panelde ajan yok; Ulak düz kabuğa yazmaz',
  'Wrong position': 'Geçersiz konum', 'No such conversation': 'Böyle bir sohbet yok', 'No conversation to search': 'Aranacak sohbet yok', 'Search for 2 to 80 characters': 'Arama 2 ile 80 karakter arasında olmalı',
  'Session is closed or unknown': 'Oturum kapalı ya da bilinmiyor', 'Message is empty': 'Mesaj boş', 'Quick-send target not found': 'Hedef oturum bulunamadı',
  'Session is no longer waiting for approval': 'Oturum artık onay beklemiyor', 'No recognised approval on screen; use the terminal': 'Ekranda tanınan bir onay yok; terminali kullan',
  'The approval screen changed; refresh': 'Onay ekranı değişti; yenile', 'This prompt has no such option': 'Bu istemde böyle bir seçenek yok',
  'A reply is already in progress': 'Bir yanıt zaten gönderiliyor', 'The confirmation is no longer valid; try again': 'İkinci onayın süresi doldu veya ekran değişti. Yeniden dene.',
  'The full command is not visible; it can only be denied': 'Komutun tamamı görünmüyor; yalnız reddedilebilir',
  'Session-wide allow is disabled for destructive commands': 'Yıkıcı komutta oturum boyu izin kapalı', 'Session-wide allow is disabled for commands with characters that cannot be seen': 'Görünmeyen karakter taşıyan komutta oturum boyu izin kapalı', 'Session is no longer waiting for anything': 'Oturum artık bir şey beklemiyor',
  'No menu to choose from on screen': 'Ekranda seçilecek bir menü yok', 'The screen changed; refresh': 'Ekran değişti; yenile', 'No such option': 'Böyle bir seçenek yok',
  'Herdr is not reachable': 'Herdr yanıt vermiyor',
  'Document not found in this workspace': 'Bu çalışma alanında belge bulunamadı', 'Unsupported document type': 'Desteklenmeyen belge türü',
  'Invalid client id': 'Geçersiz istemci kimliği', 'Invalid content length': 'Geçersiz içerik uzunluğu', 'Request body must be a JSON object': 'İstek gövdesi bir JSON nesnesi olmalı', 'Not a push subscription': 'Bu bir bildirim aboneliği değil', 'This device is not subscribed': 'Bu cihaz abone değil',
  'Request from another web page refused': 'Başka bir web sayfasından gelen istek geri çevrildi', 'Unknown host name': 'Tanınmayan sunucu adı',
  'decision must be approve, deny, or always': 'Karar onayla, reddet ya da hep izin ver olmalı',
  'Invalid key': 'Geçersiz tuş', 'Destructive command: approve from the approval card (two steps)': 'Yıkıcı komut: onay kartından iki adımla onayla',
  'Invalid tool or model': 'Araç ya da model geçersiz', 'Invalid folder': 'Klasör geçersiz', 'Invalid pin list': 'Geçersiz pin listesi', 'Tailscale identity required': 'Tailscale kimliği gerekli',
};
const detectLang = () => {
  let saved = null; try { saved = localStorage.getItem('lang'); } catch {}
  if (saved === 'tr' || saved === 'en') return saved;
  const asked = new URLSearchParams(location.search).get('lang');  // a page that links here in one language can say so
  if (asked === 'tr' || asked === 'en') return asked;
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
