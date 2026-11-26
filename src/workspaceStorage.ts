const prefix = 'jr-trading-workbench-';

export function readWorkspacePreference(key: string): string | null {
  // Read the previous branding's keys for existing watchlists and research notes.
  return localStorage.getItem(prefix + key) ?? localStorage.getItem('meridian-' + key);
}

export function writeWorkspacePreference(key: string, value: string): void {
  localStorage.setItem(prefix + key, value);
}
