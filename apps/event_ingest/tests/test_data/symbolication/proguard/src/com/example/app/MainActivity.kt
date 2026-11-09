package com.example.app

import android.app.Activity
import android.os.Bundle
import android.view.View
import android.widget.Button
import com.example.app.data.UserRepository

class MainActivity : Activity() {
    private lateinit var submitButton: Button
    private val repository = UserRepository()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.main)
        submitButton = findViewById(R.id.submit)
        submitButton.setOnClickListener(::onSubmit)
        title = repository.loadUser(1)
    }

    fun onSubmit(view: View) {
        view.isEnabled = false
        val name = repository.loadUser(-1)
        view.contentDescription = name
    }
}
