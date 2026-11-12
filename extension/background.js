// PL Hunter capture extension — service worker.
// Distila side-panel-only pattern: NO default_popup (see manifest.json).
// openPanelOnActionClick is pinned OFF so the icon click grants activeTab
// and dispatches onClicked, which opens the panel explicitly.

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: false })
    .catch((err) => console.error('setPanelBehavior failed', err));
});

chrome.action.onClicked.addListener((tab) => {
  chrome.sidePanel.open({ windowId: tab.windowId }).catch((err) => {
    console.error('sidePanel.open failed', err);
  });
});
