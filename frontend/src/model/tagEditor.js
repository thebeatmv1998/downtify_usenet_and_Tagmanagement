// Which track the "Edit tags" dialog is open for (admins only).
import { ref } from 'vue'

const track = ref(null)

function open(next) {
  track.value = next
}

function close() {
  track.value = null
}

export function useTagEditor() {
  return { track, open, close }
}
