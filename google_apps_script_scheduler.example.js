const GITHUB_OWNER = 'YOUR_GITHUB_ID';
const GITHUB_REPO = 'kis-auto-trading-template';
const WORKFLOW_FILE = 'auto-trader.yml';
const REF = 'main';

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
