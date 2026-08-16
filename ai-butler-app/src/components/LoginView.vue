<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { useAuthStore } from '@/stores/auth'

interface WechatPhoneEvent {
  detail: {
    code?: string
    errMsg?: string
  }
}

const emit = defineEmits<{ authenticated: [] }>()
const auth = useAuthStore()
const agreementAccepted = ref(false)
const phone = ref('')
const verificationCode = ref('')
const challengeId = ref<string | null>(null)
const resendSeconds = ref(0)
const sendingCode = ref(false)
const submitting = ref(false)
let countdownTimer: ReturnType<typeof setInterval> | null = null

const configReady = computed(() => auth.authConfig !== null)
const verificationEnabled = computed(() => auth.authConfig?.sms_verification_enabled === true)
const validPhone = computed(() => /^1[3-9]\d{9}$/.test(phone.value))

onMounted(() => {
  void loadConfig()
})

onUnmounted(() => {
  if (countdownTimer !== null) clearInterval(countdownTimer)
})

async function loadConfig(): Promise<void> {
  try {
    await auth.loadConfig()
  } catch {
    // 页面保留明确的重试入口；配置未知时不允许猜测验证码策略。
  }
}

function requireAgreement(): boolean {
  if (agreementAccepted.value) return true
  uni.showToast({ title: '请先阅读并同意服务协议与隐私政策', icon: 'none' })
  return false
}

function startCountdown(seconds: number): void {
  resendSeconds.value = seconds
  if (countdownTimer !== null) clearInterval(countdownTimer)
  countdownTimer = setInterval(() => {
    resendSeconds.value = Math.max(0, resendSeconds.value - 1)
    if (resendSeconds.value === 0 && countdownTimer !== null) {
      clearInterval(countdownTimer)
      countdownTimer = null
    }
  }, 1000)
}

async function sendCode(): Promise<void> {
  if (!validPhone.value) {
    uni.showToast({ title: '请输入有效的大陆手机号', icon: 'none' })
    return
  }
  sendingCode.value = true
  try {
    const response = await auth.sendPhoneVerificationCode(phone.value)
    challengeId.value = response.challenge_id
    startCountdown(response.resend_after)
    uni.showToast({ title: '验证码已发送', icon: 'success' })
  } catch (error) {
    uni.showToast({
      title: error instanceof Error ? error.message : '验证码发送失败',
      icon: 'none',
    })
  } finally {
    sendingCode.value = false
  }
}

async function phoneLogin(): Promise<void> {
  if (!requireAgreement()) return
  if (!configReady.value) {
    uni.showToast({ title: '登录配置尚未加载', icon: 'none' })
    return
  }
  if (!validPhone.value) {
    uni.showToast({ title: '请输入有效的大陆手机号', icon: 'none' })
    return
  }
  if (verificationEnabled.value && (!challengeId.value || !verificationCode.value.trim())) {
    uni.showToast({ title: '请先获取并输入验证码', icon: 'none' })
    return
  }
  submitting.value = true
  try {
    await auth.loginWithPhone(
      phone.value,
      challengeId.value ?? undefined,
      verificationCode.value.trim() || undefined,
    )
    emit('authenticated')
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '登录失败', icon: 'none' })
  } finally {
    submitting.value = false
  }
}

function miniappLoginCode(): Promise<string> {
  return new Promise((resolve, reject) => {
    uni.login({
      provider: 'weixin',
      success: (result) => resolve(result.code),
      fail: () => reject(new Error('微信登录授权失败')),
    })
  })
}

async function wechatLogin(event: WechatPhoneEvent): Promise<void> {
  if (!requireAgreement()) return
  if (!configReady.value) {
    uni.showToast({ title: '登录配置尚未加载', icon: 'none' })
    return
  }
  const phoneCode = event.detail.code
  if (!phoneCode) {
    uni.showToast({ title: '需要授权手机号后才能登录', icon: 'none' })
    return
  }
  submitting.value = true
  try {
    await auth.loginWithWechat(await miniappLoginCode(), phoneCode)
    emit('authenticated')
  } catch (error) {
    uni.showToast({ title: error instanceof Error ? error.message : '登录失败', icon: 'none' })
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <view class="auth-screen">
    <view class="auth-card">
      <view class="auth-orbit orbit-one" />
      <view class="auth-orbit orbit-two" />
      <view class="auth-brand">
        <view class="auth-logo">AI</view>
        <text class="auth-kicker">AI PERSONAL BUTLER</text>
        <text class="auth-title">你的 AI 个人管家</text>
        <text class="auth-description">一个入口，帮你规划目标、跟进任务，并在变更前征得确认。</text>
      </view>

      <view v-if="auth.configError" class="config-error">
        <text>{{ auth.configError }}</text>
        <button :disabled="auth.configLoading" @click="loadConfig">重新加载</button>
      </view>

      <!-- #ifdef H5 -->
      <view class="phone-form">
        <input
          v-model="phone"
          class="phone-input"
          type="number"
          maxlength="11"
          placeholder="请输入手机号码"
        />
        <view v-if="verificationEnabled" class="code-row">
          <input
            v-model="verificationCode"
            class="phone-input code-input"
            type="number"
            :maxlength="auth.authConfig?.sms_code_length ?? 6"
            placeholder="短信验证码"
          />
          <button
            class="send-code-button"
            :disabled="sendingCode || resendSeconds > 0 || !validPhone"
            @click="sendCode"
          >
            {{ resendSeconds > 0 ? `${resendSeconds}s` : '获取验证码' }}
          </button>
        </view>
        <button
          class="primary-login-button"
          :disabled="submitting || !configReady"
          @click="phoneLogin"
        >
          手机号登录
        </button>
      </view>
      <!-- #endif -->

      <!-- #ifdef MP-WEIXIN -->
      <button
        class="wechat-button"
        :open-type="agreementAccepted ? 'getPhoneNumber' : undefined"
        :disabled="submitting || !configReady"
        @getphonenumber="wechatLogin"
        @click="requireAgreement"
      >
        <text class="wechat-icon">微</text>
        <text>微信一键登录</text>
      </button>
      <!-- #endif -->

      <label class="agreement-row" @click="agreementAccepted = !agreementAccepted">
        <view class="agreement-check" :class="{ checked: agreementAccepted }">
          {{ agreementAccepted ? '✓' : '' }}
        </view>
        <text>我已阅读并同意《服务协议》和《隐私政策》</text>
      </label>
      <view class="auth-note"><text>i</text>手机号是唯一账号标识，令牌按平台安全策略保存</view>
    </view>
  </view>
</template>

<style scoped>
.auth-screen {
  box-sizing: border-box;
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 48rpx 32rpx;
  background:
    radial-gradient(circle at 18% 10%, rgba(89, 107, 255, 0.23), transparent 28%),
    radial-gradient(circle at 88% 84%, rgba(78, 197, 160, 0.24), transparent 30%), #eef1f7;
}

.auth-card {
  position: relative;
  box-sizing: border-box;
  width: min(410px, 100%);
  padding: 62rpx 42rpx 38rpx;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.85);
  border-radius: 48rpx;
  box-shadow: 0 42rpx 100rpx rgba(25, 34, 61, 0.17);
}

.auth-orbit {
  position: absolute;
  width: 180rpx;
  height: 180rpx;
  border: 1px solid rgba(89, 107, 255, 0.12);
  border-radius: 50%;
}

.orbit-one {
  top: -90rpx;
  right: -30rpx;
}

.orbit-two {
  top: -50rpx;
  right: 10rpx;
  width: 90rpx;
  height: 90rpx;
}

.auth-brand {
  display: flex;
  align-items: center;
  text-align: center;
  flex-direction: column;
}

.auth-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 126rpx;
  height: 126rpx;
  color: #fff;
  font-size: 40rpx;
  font-weight: 900;
  background: linear-gradient(135deg, #596bff, #4ec5a0);
  border-radius: 42rpx;
  box-shadow: 0 22rpx 44rpx rgba(89, 107, 255, 0.25);
}

.auth-kicker {
  margin-top: 32rpx;
  color: #596bff;
  font-size: 18rpx;
  font-weight: 800;
  letter-spacing: 4rpx;
}

.auth-title {
  margin-top: 12rpx;
  color: #182036;
  font-size: 42rpx;
  font-weight: 790;
}

.auth-description {
  margin: 18rpx 0 38rpx;
  color: #727b91;
  font-size: 23rpx;
  line-height: 1.7;
  text-align: center;
}

.phone-form,
.phone-form .code-row {
  display: flex;
  gap: 16rpx;
  flex-direction: column;
}

.phone-form .code-row {
  flex-direction: row;
}

.phone-input {
  box-sizing: border-box;
  width: 100%;
  height: 88rpx;
  padding: 0 26rpx;
  color: #182036;
  font-size: 25rpx;
  background: #f7f8fb;
  border: 1px solid #dfe3ed;
  border-radius: 22rpx;
}

.code-input {
  flex: 1;
  min-width: 0;
}

.send-code-button {
  width: 190rpx;
  height: 88rpx;
  color: #596bff;
  font-size: 21rpx;
  line-height: 88rpx;
  background: #eef0ff;
  border-radius: 22rpx;
}

.primary-login-button,
.wechat-button {
  display: flex;
  min-height: 88rpx;
  align-items: center;
  justify-content: center;
  gap: 14rpx;
  color: #fff;
  font-size: 25rpx;
  font-weight: 750;
  line-height: 88rpx;
  background: #596bff;
  border: 0;
  border-radius: 26rpx;
}

.wechat-button {
  background: #17b35b;
}

.wechat-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40rpx;
  height: 40rpx;
  color: #17a354;
  font-size: 17rpx;
  background: #fff;
  border-radius: 50%;
}

.agreement-row,
.auth-note {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  margin-top: 24rpx;
  color: #7b8497;
  font-size: 18rpx;
}

.agreement-check {
  width: 28rpx;
  height: 28rpx;
  color: #fff;
  line-height: 28rpx;
  text-align: center;
  border: 1px solid #c9cfdb;
  border-radius: 8rpx;
}

.agreement-check.checked {
  background: #596bff;
  border-color: #596bff;
}

.auth-note {
  margin-top: 26rpx;
  color: #8b93a3;
  font-size: 17rpx;
}

.config-error {
  margin-bottom: 22rpx;
  padding: 18rpx;
  color: #b33b45;
  font-size: 20rpx;
  text-align: center;
  background: #fff0f1;
  border-radius: 18rpx;
}

.config-error button {
  margin-top: 12rpx;
  color: #596bff;
  font-size: 19rpx;
  background: transparent;
}
</style>
