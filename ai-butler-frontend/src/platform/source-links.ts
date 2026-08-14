/**
 * 打开后端校验过的短期来源地址。H5 新窗口打开；小程序下载私有文件后交给
 * 系统阅读器，普通网页则复制地址，避免绕过小程序业务域名策略。
 */
export function openSourceLink(
  url: string,
  accessType: 'EXTERNAL_URL' | 'SIGNED_FILE',
): Promise<void> {
  // #ifdef H5
  window.open(url, '_blank', 'noopener,noreferrer')
  return Promise.resolve()
  // #endif

  // #ifndef H5
  if (accessType === 'EXTERNAL_URL') {
    return new Promise((resolve, reject) => {
      uni.setClipboardData({
        data: url,
        success: () => resolve(),
        fail: (result) => reject(new Error(result.errMsg || '复制来源地址失败')),
      })
    })
  }
  return new Promise((resolve, reject) => {
    uni.downloadFile({
      url,
      success(result) {
        if (result.statusCode < 200 || result.statusCode >= 300) {
          reject(new Error(`文件下载失败（${result.statusCode}）`))
          return
        }
        uni.openDocument({
          filePath: result.tempFilePath,
          success: () => resolve(),
          fail: (error) => reject(new Error(error.errMsg || '文件打开失败')),
        })
      },
      fail: (error) => reject(new Error(error.errMsg || '文件下载失败')),
    })
  })
  // #endif
}
