// Entry point: wires the store, the API client and the views, and routes by phase.

import { Store } from './store.js';
import { Client } from './api.js';
import { mountHeader } from './header.js';
import { createStopMenu } from './stop-menu.js';
import { createSettingsMenu } from './settings.js';
import { mountAccounts } from './accounts.js';
import { mountIntake } from './intake.js';
import { mountStack } from './stack.js';
import { mountBootstrap } from './bootstrap.js';
import { mountConfigure } from './configure.js';
import { mountBoard } from './board.js';
import { mountTaskDrawer } from './task-drawer.js';
import { mountMetrics } from './metrics.js';
import { mountPreview } from './preview.js';

const THEME_KEY = 'jbi-theme';

function resolveTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const system = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  return saved || system;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
}

function main() {
  applyTheme(resolveTheme());
  const store = new Store();
  const api = new Client(store);
  const ctx = { store, api };

  const splash = document.getElementById('splash');
  const splashText = document.getElementById('splash-text');
  const sections = [...document.querySelectorAll('.phase')];
  const previewSection = document.getElementById('phase-preview');
  const metricsSection = document.getElementById('phase-metrics');
  let tasksRef = null;
  const previewPage = mountPreview(ctx, previewSection, { onOpenTask: (id) => tasksRef?.open(id) });
  const metricsPage = mountMetrics(ctx, metricsSection);
  let mounted = false;
  let activeOverlay = (location.pathname === '/metrics' || location.hash === '#metrics') ? 'metrics'
    : (location.pathname === '/preview' || location.hash === '#preview') ? 'preview'
    : null;

  function syncSections() {
    if (activeOverlay === 'metrics') {
      splash.hidden = true;
      for (const section of sections) section.hidden = section !== metricsSection;
      metricsPage.refresh();
      return;
    }
    if (activeOverlay === 'preview') {
      splash.hidden = true;
      for (const section of sections) section.hidden = section !== previewSection;
      previewPage.refresh();
      return;
    }
    if (!store.state?.run) return;
    splash.hidden = true;
    const phase = store.state.run.phase;
    for (const section of sections) {
      const show = section.dataset.phase.split(' ').includes(phase);
      if (section.hidden === show) section.hidden = !show;
    }
    document.body.dataset.phase = phase;
  }

  function toggleOverlay(which) {
    activeOverlay = activeOverlay === which ? null : which;
    syncSections();
  }

  function mountViews() {
    mounted = true;
    if (!activeOverlay && store.state?.config?.preview) {
      activeOverlay = 'preview';
    }
    const accounts = mountAccounts(ctx);
    const header = mountHeader(ctx, {
      onAccounts: accounts.open,
      onPreview: () => toggleOverlay('preview'),
      onMetrics: () => toggleOverlay('metrics'),
      onTheme: () => {
        const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        localStorage.setItem(THEME_KEY, next);
        applyTheme(next);
        header.setThemeIcon(next === 'dark');
      },
      settingsMenu: createSettingsMenu(ctx),
      stopMenu: createStopMenu(ctx),
    });
    header.setThemeIcon(document.documentElement.dataset.theme === 'dark');
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!localStorage.getItem(THEME_KEY)) { applyTheme(resolveTheme()); header.setThemeIcon(resolveTheme() === 'dark'); }
    });

    const tasks = mountTaskDrawer(ctx);
    tasksRef = tasks;
    const byId = (id) => document.getElementById(id);
    mountIntake(ctx, byId('phase-intake'));
    mountStack(ctx, byId('phase-stack'));
    mountBootstrap(ctx, byId('phase-bootstrap'));
    mountConfigure(ctx, byId('phase-configure'));
    mountBoard(ctx, byId('phase-board'), { onOpenTask: tasks.open });

    store.subscribe(['run'], () => {
      syncSections();
    });
  }

  if (activeOverlay) {
    syncSections();
  }

  store.subscribe(['$connection'], () => {
    const value = store.local.connection;
    if (value === 'unauthenticated' && !activeOverlay) {
      splash.hidden = false;
      for (const section of sections) section.hidden = true;
      document.getElementById('run-name').textContent = 'Signed out';
      splashText.textContent = 'Open the link that the CLI printed to sign in. A link works once; the CLI menu prints a new one.';
    }
  });

  // Views mount on the first snapshot, so each one renders from real state.
  const stopWaiting = store.subscribe(['*'], () => {
    if (!mounted && store.ready) {
      mountViews();
      queueMicrotask(stopWaiting);
    }
  });

  api.start();
}

main();
