export function elapsedLabel(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))} 毫秒`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${Math.floor(seconds % 60)} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

// 服务端无时区时间是上海本地时间，避免被其他时区的浏览器误读。
export function serverTime(value: string): number {
  return Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}+08:00`);
}

export function fileSizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}
