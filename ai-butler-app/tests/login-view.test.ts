import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  fileURLToPath(new URL('../src/components/LoginView.vue', import.meta.url)),
  'utf8',
)
const template = parse(source).descriptor.template?.content ?? ''

describe('platform login entry', () => {
  it('keeps H5 on the phone-only flow', () => {
    const h5 = template.slice(
      template.indexOf('<!-- #ifdef H5 -->'),
      template.indexOf('<!-- #endif -->'),
    )
    expect(h5).toContain('请输入手机号码')
    expect(h5).toContain('verificationEnabled')
    expect(h5).not.toContain('getPhoneNumber')
  })

  it('requires miniapp phone authorization without a phone fallback', () => {
    const start = template.indexOf('<!-- #ifdef MP-WEIXIN -->')
    const miniapp = template.slice(start, template.indexOf('<!-- #endif -->', start))
    expect(miniapp).toContain("'getPhoneNumber'")
    expect(miniapp).toContain('@getphonenumber="wechatLogin"')
    expect(miniapp).not.toContain('phoneLogin')
  })
})
