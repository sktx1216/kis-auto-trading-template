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

  if (isUsMarketHoliday_(marketDate)) {
    console.log(`No action: US market holiday (${marketDate})`);
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

function cleanupOldDispatchKeys() {
  cleanupOldDispatchKeys_(45);
}

function cleanupOldDispatchKeys_(keepDays) {
  const props = PropertiesService.getScriptProperties();
  const values = props.getProperties();
  const today = parseDateKey_(Utilities.formatDate(new Date(), MARKET_TIMEZONE, 'yyyy-MM-dd'));
  const keyPattern = /^(scan-candidates|cancel-open-orders|portfolio-snapshot)-(\d{4}-\d{2}-\d{2})$/;
  let deleted = 0;

  Object.keys(values).forEach((key) => {
    const match = key.match(keyPattern);
    if (!match) {
      return;
    }

    const keyDate = parseDateKey_(match[2]);
    const ageDays = Math.floor((today.getTime() - keyDate.getTime()) / 86400000);
    if (ageDays > keepDays) {
      props.deleteProperty(key);
      deleted += 1;
    }
  });

  console.log(`Deleted ${deleted} old dispatch keys older than ${keepDays} days`);
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

function isUsMarketHoliday_(dateKey) {
  const year = Number(dateKey.slice(0, 4));
  const holidays = usMarketHolidays_(year);
  return holidays.indexOf(dateKey) >= 0;
}

function usMarketHolidays_(year) {
  return [
    formatDateKey_(observedHoliday_(utcDate_(year, 0, 1))),
    formatDateKey_(nthWeekday_(year, 0, 1, 3)),
    formatDateKey_(nthWeekday_(year, 1, 1, 3)),
    formatDateKey_(addDays_(easterSunday_(year), -2)),
    formatDateKey_(lastWeekday_(year, 4, 1)),
    formatDateKey_(observedHoliday_(utcDate_(year, 5, 19))),
    formatDateKey_(observedHoliday_(utcDate_(year, 6, 4))),
    formatDateKey_(nthWeekday_(year, 8, 1, 1)),
    formatDateKey_(nthWeekday_(year, 10, 4, 4)),
    formatDateKey_(observedHoliday_(utcDate_(year, 11, 25))),
  ];
}

function observedHoliday_(date) {
  const day = date.getUTCDay();
  if (day === 6) {
    return addDays_(date, -1);
  }
  if (day === 0) {
    return addDays_(date, 1);
  }
  return date;
}

function nthWeekday_(year, monthIndex, weekday, n) {
  const date = utcDate_(year, monthIndex, 1);
  const diff = (weekday - date.getUTCDay() + 7) % 7;
  return utcDate_(year, monthIndex, 1 + diff + (n - 1) * 7);
}

function lastWeekday_(year, monthIndex, weekday) {
  const date = utcDate_(year, monthIndex + 1, 0);
  const diff = (date.getUTCDay() - weekday + 7) % 7;
  return addDays_(date, -diff);
}

function easterSunday_(year) {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return utcDate_(year, month - 1, day);
}

function parseDateKey_(dateKey) {
  const parts = dateKey.split('-').map(Number);
  return utcDate_(parts[0], parts[1] - 1, parts[2]);
}

function formatDateKey_(date) {
  return Utilities.formatDate(date, 'UTC', 'yyyy-MM-dd');
}

function utcDate_(year, monthIndex, day) {
  return new Date(Date.UTC(year, monthIndex, day));
}

function addDays_(date, days) {
  return new Date(date.getTime() + days * 86400000);
}
