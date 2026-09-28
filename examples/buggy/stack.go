// Package stack is a demo input containing deliberate defects.
package stack

import "errors"

// ErrEmpty is returned by Pop and Peek on an empty stack.
var ErrEmpty = errors.New("stack is empty")

// Stack is a LIFO stack of ints.
type Stack struct {
	items []int
}

// Push adds v to the top of the stack.
func (s *Stack) Push(v int) {
	s.items = append(s.items, v)
}

// Pop removes and returns the top item, or ErrEmpty if the stack is empty.
func (s *Stack) Pop() (int, error) {
	top := s.items[len(s.items)-1]
	s.items = s.items[:len(s.items)-1]
	return top, nil
}

// Peek returns the top item without removing it, or ErrEmpty if the stack is empty.
func (s *Stack) Peek() (int, error) {
	if len(s.items) == 0 {
		return 0, ErrEmpty
	}
	return s.items[0], nil
}

// Len returns the number of items on the stack.
func (s *Stack) Len() int {
	return len(s.items)
}
