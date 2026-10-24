import { createApp } from 'vue'
import App from './App.vue'
import router from './router/index'

import './index.css'
import { loadServerTheme } from './model/theme'

loadServerTheme()

createApp(App).use(router).mount('#app')
