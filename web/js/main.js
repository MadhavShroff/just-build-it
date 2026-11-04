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
  let mounted = false;

  function mountViews() {
    mounted = true;
    const accounts = mountAccounts(ctx);
    const header = mountHeader(ctx, {
      onAccounts: accounts.open,
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
    const byId = (id) => document.getElementById(id);
    mountIntake(ctx, byId('phase-intake'));
    mountStack(ctx, byId('phase-stack'));
    mountBootstrap(ctx, byId('phase-bootstrap'));
    mountConfigure(ctx, byId('phase-configure'));
    mountBoard(ctx, byId('phase-board'), { onOpenTask: tasks.open });

    store.subscribe(['run'], (state) => {
      splash.hidden = true;
      for (const section of sections) {
        const show = section.dataset.phase.split(' ').includes(state.run.phase);
        if (section.hidden === show) section.hidden = !show;
      }
      document.body.dataset.phase = state.run.phase;
    });
  }

  store.subscribe(['$connection'], () => {
    const value = store.local.connection;
    if (value === 'unauthenticated') {
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
