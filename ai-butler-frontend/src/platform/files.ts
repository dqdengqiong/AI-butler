import { bytesToHex } from '@noble/hashes/utils'
import { sha256 } from '@noble/hashes/sha256'

export interface SelectedFile {
  name: string
  mimeType: string
  bytes: Uint8Array
  sha256: string
}

interface BrowserFileLike {
  name?: string
  type?: string
  size?: number
  path?: string
  arrayBuffer?: () => Promise<ArrayBuffer>
}

function mimeFromName(name: string): string {
  const extension = name.split('.').pop()?.toLowerCase()
  return (
    {
      pdf: 'application/pdf',
      png: 'image/png',
      jpg: 'image/jpeg',
      jpeg: 'image/jpeg',
      webp: 'image/webp',
      txt: 'text/plain',
      md: 'text/markdown',
      csv: 'text/csv',
      docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }[extension ?? ''] ?? 'application/octet-stream'
  )
}

function readPlatformPath(path: string): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const platform = uni as unknown as {
      getFileSystemManager(): {
        readFile(options: {
          filePath: string
          success(result: { data: string | ArrayBuffer }): void
          fail(result: { errMsg?: string }): void
        }): void
      }
    }
    platform.getFileSystemManager().readFile({
      filePath: path,
      success: (result) => {
        if (result.data instanceof ArrayBuffer) resolve(result.data)
        else reject(new Error('平台没有返回文件字节'))
      },
      fail: (result) => reject(new Error(result.errMsg || '读取文件失败')),
    })
  })
}

/** 选择一个文件并在本地计算哈希；原始字节不会写入日志或普通状态存储。 */
export async function chooseFile(): Promise<SelectedFile> {
  const result = await uni.chooseFile({ count: 1, type: 'all' })
  const candidates = Array.isArray(result.tempFiles) ? result.tempFiles : [result.tempFiles]
  const candidate = candidates[0] as BrowserFileLike | undefined
  if (!candidate) throw new Error('没有选择文件')
  const name = candidate.name || candidate.path?.split('/').pop() || 'attachment.bin'
  const buffer = candidate.arrayBuffer
    ? await candidate.arrayBuffer()
    : candidate.path
      ? await readPlatformPath(candidate.path)
      : null
  if (!buffer) throw new Error('当前平台无法读取该文件')
  const bytes = new Uint8Array(buffer)
  if (bytes.byteLength === 0 || bytes.byteLength > 20 * 1024 * 1024) {
    throw new Error('文件大小必须在 1B 至 20MB 之间')
  }
  return {
    name,
    mimeType: candidate.type || mimeFromName(name),
    bytes,
    sha256: bytesToHex(sha256(bytes)),
  }
}
