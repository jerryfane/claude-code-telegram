import { createRouter, createWebHistory } from 'vue-router'
import Terminal from './views/Terminal.vue'

const routes = [
  { path: '/', redirect: '/terminal' },
  { path: '/terminal', component: Terminal },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
