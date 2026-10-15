// 前端登录态与鉴权头（App.vue 与 GovernanceBoard.vue 共用一份，避免各写一遍）。
//
// 后端两种凭据来源都通过同一个请求头 `X-API-Token` 传入：
//   1. 登录签发的会话 token（POST /api/auth/login）→ 后端解析出具体用户与角色；
//   2. 静态 SQLPA_API_TOKENS 部署时的固定凭据（粘进输入框即可）。
// 未配置任何凭据时后端按最小权限默认角色放行，此模块不设 token 也能正常工作。
import { ref } from 'vue'

const store = typeof localStorage !== 'undefined' ? localStorage : null

export const authToken = ref(store ? store.getItem('sqlpa_token') || '' : '')
export const authUser = ref(null)   // { username, role, source }

/** 统一鉴权头：所有后端请求都应带上（含 SSE 与文件流下载）。 */
export function authHeaders(extra = {}) {
  const h = { ...extra }
  if (authToken.value) h['X-API-Token'] = authToken.value
  return h
}

export function setToken(tok, user) {
  authToken.value = tok || ''
  if (store) {
    if (tok) store.setItem('sqlpa_token', tok)
    else store.removeItem('sqlpa_token')
  }
  if (user !== undefined) authUser.value = user
}

/** 查询当前身份；未登录且后端未开鉴权时返回 anonymous/默认角色（不报错）。 */
export async function fetchMe() {
  try {
    const r = await fetch('/api/auth/me', { headers: authHeaders() })
    if (!r.ok) { authUser.value = null; return null }
    authUser.value = await r.json()
    return authUser.value
  } catch {
    authUser.value = null
    return null
  }
}

/** 账号口令登录：成功即把会话 token 与身份写入本地。 */
export async function login(username, password) {
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail || ('登录失败 HTTP ' + r.status))
  setToken(j.token, { username: j.username, role: j.role, source: 'session' })
  return j
}

/** 登出：撤销会话 token 并清本地状态（静态凭据无法在服务端撤销，仅本地丢弃）。 */
export async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', headers: authHeaders() })
  } catch { /* 网络失败不阻塞本地登出 */ }
  setToken('', null)
}

/** 兼容静态 SQLPA_API_TOKENS 部署：直接粘贴固定凭据。 */
export async function useStaticToken(tok) {
  setToken((tok || '').trim(), null)
  return fetchMe()
}
