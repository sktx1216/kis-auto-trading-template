const GITHUB_OWNER = 'YOUR_GITHUB_ID';
const GITHUB_REPO = 'kis-auto-trading-template';
const WORKFLOW_FILE = 'auto-trader.yml';
const REF = 'main';
const MARKET_TIMEZONE = 'America/New_York';

function runFull() {
  dispatchWorkflow_('full');
}

function runSellOnly() {
  dispatchWorkflow_('sell-only');
}

function runScanCandidates() {
  dispatchWorkflow_('scan-candidates');
}

function runTradeFromCandidates() {
  dispatchWorkflow_('trade-from-candidates');
}

function runDiagnose() {
  dispatchWorkflow_('diagnose');
}

function runCancelOpenOrders() {
  dispatchWorkflow_('cancel-open-orders');
}

function runPortfolioSnapshot() {
  dispatchWorkflow_('portfolio-snapshot');
}

function runAutoScheduler() {
  const now = new Date();
  const marketDate = Utilities.formatDate(now, MARKET_TIMEZONE, 'yyyy-MM-dd');
  const marketDay = Utilities.formatDate(now, MARKET_TIMEZONE, 'EEE');
  const hhmm = Number(Utilities.formatDate(now, MARKET_TIMEZONE, 'HHmm'));

  if (marketDay === 'Sat' || marketDay === 'Sun') {
    console.log(`No action: weekend in New York (${marketDate})`);
    return;
  }

  if (hhmm < 930 || hhmm >= 1700) {
    console.log(`No action: outside automation window in New York (${marketDate} ${hhmm})`);
    return;
  }

  if (hhmm >= 930 && hhmm < 1100) {
    dispatchWorkflow_('sell-only');
    return;
  }

  if (hhmm >= 1100 && hhmm < 1130) {
    dispatchOncePerMarketDate_('scan-candidates', `scan-candidates-${marketDate}`);
    return;
  }

  if (hhmm >= 1130 && hhmm < 1530) {
    dispatchWorkflow_('trade-from-candidates');
    return;
  }

  if (hhmm >= 1530 && hhmm < 1600) {
    dispatchOncePerMarketDate_('cancel-open-orders', `cancel-open-orders-${marketDate}`);
    return;
  }

  if (hhmm >= 1600 && hhmm < 1700) {
    dispatchOncePerMarketDate_('portfolio-snapshot', `portfolio-snapshot-${marketDate}`);
    return;
  }

  console.log(`No action at New York time ${marketDate} ${hhmm}`);
}

function dispatchOncePerMarketDate_(mode, key) {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty(key)) {
    console.log(`Already dispatched ${mode}: ${key}`);
    return;
  }

  dispatchWorkflow_(mode);
  props.setProperty(key, new Date().toISOString());
}

function dispatchWorkflow_(mode) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('Missing script property: GITHUB_TOKEN');
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({
      ref: REF,
      inputs: { mode },
    }),
    muteHttpExceptions: true,
  });

  const status = response.getResponseCode();
  if (status !== 204) {
    throw new Error(`GitHub workflow dispatch failed: HTTP ${status} ${response.getContentText()}`);
  }

  console.log(`Dispatched ${mode} workflow at ${new Date().toISOString()}`);
}
