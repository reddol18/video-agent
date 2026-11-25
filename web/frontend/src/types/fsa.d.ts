// File System Access API 타입 보완 (일부 브라우저 타입 미포함)
interface FileSystemDirectoryHandle {
  queryPermission(desc: { mode: "read" | "readwrite" }): Promise<"granted" | "denied" | "prompt">;
  requestPermission(desc: { mode: "read" | "readwrite" }): Promise<"granted" | "denied" | "prompt">;
  entries(): AsyncIterableIterator<[string, FileSystemHandle]>;
}

interface Window {
  showDirectoryPicker(options?: {
    id?: string;
    mode?: "read" | "readwrite";
    startIn?: "desktop" | "documents" | "downloads" | "music" | "pictures" | "videos";
  }): Promise<FileSystemDirectoryHandle>;
}
