import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { initTheme } from './theme'
import './style.css'

// 在挂载前应用主题，避免首帧闪烁
initTheme()

createApp(App).use(router).mount('#app')